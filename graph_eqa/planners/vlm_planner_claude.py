import json
from enum import Enum
import time
import base64

import anthropic
import os
from typing import List, Union
import mimetypes
from graph_eqa.utils.data_utils import get_latest_image
from pydantic import BaseModel

'''
Structured output resources for Claude API:
1. https://murraycole.com/posts/claude-tool-use-pydantic
2. https://github.com/anthropics/anthropic-cookbook/blob/main/tool_use/tool_use_with_pydantic.ipynb
'''

client = anthropic.Anthropic(
    api_key=os.environ["ANTHROPIC_API_KEY"],  # This is the default and can be omitted
)


def encode_image(image_path):
  with open(image_path, "rb") as image_file:
    return base64.b64encode(image_file.read()).decode('utf-8')

def create_planner_response(frontier_node_list, room_node_list, region_node_list, object_node_list, Answer_options, use_image=True):
    class Goto_frontier_node_step(BaseModel):
        explanation_frontier: str
        frontier_id: frontier_node_list

    class Goto_object_node_step(BaseModel):
        explanation_room: str
        # explanation_region: str
        explanation_obj: str
        room_id: room_node_list
        # region_id: region_node_list
        object_id: object_node_list
    
    class Answer(BaseModel):
        explanation_ans: str
        answer: Answer_options
        explanation_conf: str
        confidence_level: float
        is_confident: bool

    class PlannerResponse(BaseModel):
        steps: List[Union[Goto_object_node_step, Goto_frontier_node_step]]
        answer: Answer
        image_description: str
        scene_graph_description: str
    
    class PlannerResponseNoFrontiers(BaseModel):
        steps: List[Goto_object_node_step]
        answer: Answer
        image_description: str
        scene_graph_description: str
    
    class PlannerResponseNoImage(BaseModel):
        steps: List[Union[Goto_object_node_step, Goto_frontier_node_step]]
        answer: Answer
        scene_graph_description: str
    
    class PlannerResponseNoFrontiersNoImage(BaseModel):
        steps: List[Goto_object_node_step]
        answer: Answer
        scene_graph_description: str
    
    if use_image:
        if frontier_node_list is None:
            return PlannerResponseNoFrontiers
        else:
            return PlannerResponse
    else:
        if frontier_node_list is None:
            return PlannerResponseNoFrontiersNoImage
        else:
            return PlannerResponseNoImage

class VLMPlannerEQAClaude:
    def __init__(self, cfg, sg_sim, question, pred_candidates, choices, answer, output_path):
        
        self._question, self.choices, self.vlm_pred_candidates = question, choices, pred_candidates
        self._answer = answer
        self._output_path = output_path
        self._vlm_type = cfg.name
        self._use_image = cfg.use_image

        self._example_plan = '' #TODO(saumya)
        self._history = ''
        self.full_plan = ''
        self._t = 0
        self._add_history = cfg.add_history

        self._outputs_to_save = [f'Question: {self._question}. \n Answer: {self._answer} \n']
        self.sg_sim = sg_sim
        #self.temp = cfg.temp

    @property
    def t(self):
        return self._t
    
    def get_actions(self): 
        object_node_list = Enum('object_node_list', {id: id for id, name in zip(self.sg_sim.object_node_ids, self.sg_sim.object_node_names)}, type=str)
        
        if len(self.sg_sim.frontier_node_ids)> 0:
            frontier_node_list = Enum('frontier_node_list', {ac: ac for ac in self.sg_sim.frontier_node_ids}, type=str)
        else:
            frontier_node_list = Enum('frontier_node_list', {'frontier_0': 'Do not choose this option. No more frontiers left.'}, type=str)
        room_node_list = Enum('room_node_list', {id: name for id, name in zip(self.sg_sim.room_node_ids, self.sg_sim.room_node_names)}, type=str)
        region_node_list = Enum('region_node_list', {ac: ac for ac in self.sg_sim.region_node_ids}, type=str)
        Answer_options = Enum('Answer_options', {token: token for token, choice in zip(self.vlm_pred_candidates, self.choices)}, type=str)
        return frontier_node_list, room_node_list, region_node_list, object_node_list, Answer_options
    
    
    @property
    def agent_role_prompt(self):
        scene_graph_desc = "A scene graph represents an indoor environment in a hierarchical tree structure consisting of nodes and edges/links. There are six types of nodes: building, rooms, visited areas, frontiers, objects, and agent in the environemnt. \n \
            The tree structure is as follows: At the highest level 5 is a 'building' node. \n \
            At level 4 are room nodes. There are links connecting the building node to each room node. \n \
            At the lower level 3, are region and frontier nodes. 'region' node represent region of room that is already explored. Frontier nodes represent areas that are at the boundary of visited and unexplored areas. There are links from room nodes to corresponding region and frontier nodes depicted which room they are located in. \n \
            At the lowest level 2 are object nodes and agent nodes. There is an edge from region node to each object node depicting which visited area of which room the object is located in. \
            There are also links between frontier nodes and objects nodes, depicting the objects in the vicinity of a frontier node. \n \
            Finally the agent node is where you are located in the environment. There is an edge between a region node and the agent node, depicting which visited area of which room the agent is located in."
        current_state_des = "'CURRENT STATE' will give you the exact location of the agent in the scene graph by giving you the agent node id, location, room_id and room name. "
        
        if self._use_image:
            current_state_des += " Additionally, you will also be given the current view of the agent as an image. "
        
        prompt = f'''You are an excellent hierarchical graph planning agent. 
            Your goal is to navigate an unseen environment to confidently answer a multiple-choice question about the environment.
            As you explore the environment, your sensors are building a scene graph representation (in json format) and you have access to that scene graph.  
            {scene_graph_desc}. {current_state_des} 
            Given the current state information, try to answer the question. Explain the reasoning for selecting the answer.
            Finally, report whether you are confident in answering the question. 
            Explain the reasoning behind the confidence level of your answer. Rate your level of confidence. 
            Provide a value between 0 and 1; 0 for not confident at all and 1 for absolutely certain.
            Do not use just commensense knowledge to decide confidence. 
            Choose TRUE, if you have explored enough and are certain about answering the question correctly and no further exploration will help you answer the question better. 
            Choose 'FALSE', if you are uncertain of the answer and should explore more to ground your answer in the current envioronment. 
            Clarification: This is not your confidence in choosing the next action, but your confidence in answering the question correctly.
            If you are unable to answer the question with high confidence, and need more information to answer the question, then you can take two kinds of steps in the environment: Goto_object_node_step or Goto_frontier_node_step 
            You also have to choose the next action, one which will enable you to answer the question better. 
            Goto_object_node_step: Navigates near a certain object in the scene graph. Choose this action to get a good view of the region aroung this object, if you think going near this object will help you answer the question better.
            Important to note, the scene contains incomplete information about the environment (objects maybe missing, relationships might be unclear), so it is useful to go near relevant objects to get a better view to answer the question. 
            Use a scene graph as an imperfect guide to lead you to relevant regions to inspect.
            Choose the object in a hierarchical manner by first reasoning about which room you should goto to best answer the question, and then choose the specific object. \n
            Goto_frontier_node_step: If you think that using action "Goto_object_node_step" is not useful, in other words, if you think that going near any of the object nodes in the current scene graph will not provide you with any useful information to answer the question better, then choose this action.
            This action will navigate you to a frontier (unexplored) region of the environment and will provide you information about new objects/rooms not yet in the scene graph. It will expand the scene graph. 
            Choose this frontier based on the objects connected this frontier, in other words, Goto the frontier near which you see objects that are useful for answering the question or seem useful as a good exploration direction. Explain reasoning for choosing this frontier, by listing the list of objects (<id> and <name>) connected to this frontier node via a link (refer to scene graph) \n \
            
            While choosing either of the above actions, play close attention to 'HISTORY' especially the previous 'Action's to see if you have taken the same action at previous timesteps. 
            Avoid taking the same actions you have taken before.
            Describe the CURRENT IMAGE. Pay special attention to features that can help answer the question or select future actions.
            Describe the SCENE GRAPH. Pay special attention to features that can help answer the question or select future actions.
            '''
        
        prompt_no_image = f'''You are an excellent hierarchical graph planning agent. 
            Your goal is to navigate an unseen environment to confidently answer a multiple-choice question about the environment.
            As you explore the environment, your sensors are building a scene graph representation (in json format) and you have access to that scene graph.  
            {scene_graph_desc}. {current_state_des} 
            Given the current state information, try to answer the question. Explain the reasoning for selecting the answer.
            Finally, report whether you are confident in answering the question. 
            Explain the reasoning behind the confidence level of your answer. Rate your level of confidence. 
            Provide a value between 0 and 1; 0 for not confident at all and 1 for absolutely certain.
            Do not use just commensense knowledge to decide confidence. 
            Choose TRUE, if you have explored enough and are certain about answering the question correctly and no further exploration will help you answer the question better. 
            Choose 'FALSE', if you are uncertain of the answer and should explore more to ground your answer in the current envioronment. 
            Clarification: This is not your confidence in choosing the next action, but your confidence in answering the question correctly.
            If you are unable to answer the question with high confidence, and need more information to answer the question, then you can take two kinds of steps in the environment: Goto_object_node_step or Goto_frontier_node_step 
            You also have to choose the next action, one which will enable you to answer the question better. 
            Goto_object_node_step: Navigates near a certain object in the scene graph. Choose this action to go to the region aroung this object, if you think going near this object will help you answer the question better.
            Choose the object in a hierarchical manner by first reasoning about which room you should goto to best answer the question, and then choose the specific object. \n
            Goto_frontier_node_step: If you think that using action "Goto_object_node_step" is not useful, in other words, if you think that going near any of the object nodes in the current scene graph will not provide you with any useful information to answer the question better, then choose this action.
            This action will navigate you to a frontier (unexplored) region of the environment and will provide you information about new objects/rooms not yet in the scene graph. It will expand the scene graph. 
            Choose this frontier based on the objects connected this frontier, in other words, Goto the frontier near which there are objects useful for answering the question or seem useful as a good exploration direction. Explain reasoning for choosing this frontier, by listing the list of objects (<id> and <name>) connected to this frontier node via a link (refer to scene graph) \n \
            
            While choosing either of the above actions, play close attention to 'HISTORY' especially the previous 'Action's to see if you have taken the same action at previous timesteps. 
            Avoid taking the same actions you have taken before.
            Describe the SCENE GRAPH. Pay special attention to features that can help answer the question or select future actions.
            '''
        
        if self._use_image:
            return prompt
        else:
            return prompt_no_image

    def get_current_state_prompt(self, scene_graph, agent_state):
        # TODO(saumya): Include history
        prompt = f"At t = {self.t}: \n \
            CURRENT AGENT STATE: {agent_state}. \n \
            SCENE GRAPH: {scene_graph}. \n "

        if self._add_history:
            prompt += f"HISTORY: {self._history}"

        return prompt

    def update_history(self, agent_state, step, answer):
        if (step['step_type'] == 'Goto_object_node_step'):
            action = f"Goto object_id: {step['choice']}"
        elif step['step_type'] == 'Goto_frontier_node_step':
            action = f"Goto frontier_id: {step['choice']}"
        else:
            # Need to add actual choice answer here rather than just the letter
            action = f"Answer: {step['choice']}, Confident: {step['is_confident']}, Confidence level:{step['confidence_level']}"
        
        last_step = f'''
            [Agent state(t={self.t}): {agent_state}, 
            Action(t={self.t}): {action},
            Answer(t={self.t}): {answer['answer']}
            Confidence(t={self.t}):  Confident: {answer['is_confident']}, Confidence level:{answer['confidence_level']}  \n
        '''
        self._history += last_step
    
    def get_claude_output(self, current_state_prompt):
        messages=[
            {"role": "user", "content": f"AGENT ROLE: {self.agent_role_prompt}"},
            {"role": "user", "content": f"QUESTION: {self._question}"},
            {"role": "user", "content": f"CURRENT STATE: {current_state_prompt}."},
        ]

        if self._use_image:
            image_path = get_latest_image(self._output_path)
            base64_image = encode_image(image_path) 
            mime_type = mimetypes.guess_type(image_path)[0]

            image_message = {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": base64_image,
                            },
                        },
                        {
                        "type": "text",
                        "text": "CURRENT IMAGE: This image represents the current view of the agent. Use this as additional information to answer the question."
                        }
                    ],
                }
            
            messages.append(image_message)

        frontier_node_list, room_node_list, region_node_list, object_node_list, Answer_options = self.get_actions()

        tools = [
            {
                "name": "planner_response",
                "description": "The Claude VLM Planner response.",
                "input_schema": create_planner_response(
                        frontier_node_list, 
                        room_node_list, 
                        region_node_list, 
                        object_node_list, 
                        Answer_options).model_json_schema()
            }
        ]
        
        succ=False
        while not succ:
            try:
                start = time.time()
                # TODO write Claude's response here
                response = client.messages.create(
                    model=self._vlm_type,
                    temperature=0.4,
                    max_tokens=4096,
                    messages=messages,
                    tools=tools,
                    tool_choice={"type": "tool", "name": "planner_response"}
                )

                print(f"Time taken for planning next step: {time.time()-start}s")
                # Make sure Claude responds with an input and that it is using the tools we provided
                if (response.content[0].input and response.content[0].type == 'tool_use'): 
                    succ=True
            except Exception as e:
                print(f"An error occurred: {e}. Sleeping for 45s")
                time.sleep(45)
        
        
        response_dict = response.content[0].input
        
        if len(response_dict['steps']) > 0 and 'scene_graph_description' in response_dict.keys() and 'answer' in response_dict.keys():
            step_out = response_dict["steps"][0]
            sg_desc = response_dict["scene_graph_description"]
            if self._use_image:
                img_desc = response_dict["image_description"]
            else:
                img_desc = ' '

            answer = response_dict['answer']

            if step_out:
                step = {}
                if 'frontier_id' in step_out.keys():
                    step_type = 'Goto_frontier_node_step'
                elif 'object_id' in step_out.keys():
                    step_type = 'Goto_object_node_step'
                step['step_type'] = step_type
                if (step_type == 'Goto_object_node_step'):
                    step['choice'] = step_out['object_id']
                    #step['value']= step_out['object_name']
                    step['explanation'] = step_out['explanation_obj']
                    step['room'] = step_out['room_id']
                    step['explanation_room'] = step_out['explanation_room']
                elif (step_type == 'Goto_frontier_node_step'):
                    step['choice'] = step_out['frontier_id']
                    step['explanation'] = step_out['explanation_frontier']
            else:
                step = None

            return step, answer, img_desc, sg_desc
        else:
            step = None
            answer = None
            img_desc = 'No image description available.'
            sg_desc = 'No scene graph description available.'
         
        return step, answer, img_desc, sg_desc
    

    def get_next_action(self):        
        agent_state = self.sg_sim.get_current_semantic_state_str()
        current_state_prompt = self.get_current_state_prompt(self.sg_sim.scene_graph_str, agent_state)

        sg_desc=''
        step, answer, img_desc, sg_desc = self.get_claude_output(current_state_prompt)

        print(f'At t={self._t}: \n Step: {step}, \n Answer: {answer}')
        # Saving outputs to file
        self._outputs_to_save.append(f'At t={self._t}: \n \
                                        Agent state: {agent_state} \n \
                                        VLM step: {step} \n \
                                        Image desc: {img_desc} \n \
                                        Scene graph desc: {sg_desc}')
        self.full_plan = ' '.join(self._outputs_to_save)
        with open(self._output_path / "llm_outputs.json", "w") as text_file:
            text_file.write(self.full_plan)

        if step is None or step['choice'] == 'Do not choose this option. No more frontiers left.':
            return None, None, False, 0., " "
        
        if answer is None:
            return None, None, False, 0., " "

        if self._add_history:
            self.update_history(agent_state, step, answer)

        self._t += 1

        # if step['step_type'] == 'answer':
        #     return None, None, step['is_confident'], step['confidence_level'], step['choice']
        # else:
        target_pose = self.sg_sim.get_position_from_id(step['choice'])
        target_id = step['choice']
        return target_pose, target_id, answer['is_confident'], answer['confidence_level'], answer['answer']