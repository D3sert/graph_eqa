import re, json
from enum import Enum
from typing import List, Union
import time
import base64

from openai import OpenAI
from graph_eqa.utils.data_utils import get_latest_image
from pydantic import BaseModel, ValidationError

from together import Together

client = Together()

def encode_image(image_path):
  with open(image_path, "rb") as image_file:
    return base64.b64encode(image_file.read()).decode('utf-8')

# TODO: implement this for Llama 4 using guidance-ai
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


class VLMPlannerEQALlama4:
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

    @property
    def t(self):
        return self._t

    def get_actions(self):
        #object_node_list = Enum('object_node_list', {id: name for id, name in zip(self.sg_sim.object_node_ids, self.sg_sim.object_node_names)}, type=str)
        object_node_list = Enum('object_node_list', {name: id for id, name in zip(self.sg_sim.object_node_ids, self.sg_sim.object_node_names)}, type=str)

        if len(self.sg_sim.frontier_node_ids)> 0:
            frontier_node_list = Enum('frontier_node_list', {ac: ac for ac in self.sg_sim.frontier_node_ids}, type=str)
        else:
            frontier_node_list = Enum('frontier_node_list', {'frontier_0': 'Do not choose this option. No more frontiers left.'}, type=str)
            #frontier_node_list = None

        #room_node_list = Enum('room_node_list', {id: name for id, name in zip(self.sg_sim.room_node_ids, self.sg_sim.room_node_names)}, type=str)
        room_node_list = Enum('room_node_list', {name: id for id, name in zip(self.sg_sim.room_node_ids, self.sg_sim.room_node_names)}, type=str)
        region_node_list = Enum('region_node_list', {ac: ac for ac in self.sg_sim.region_node_ids}, type=str)
        Answer_options = Enum('Answer_options', {token: choice for token, choice in zip(self.vlm_pred_candidates, self.choices)}, type=str)
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
            For particularly obvious or challenging questions, consider finding a balance between answering too early and incorrectly, and taking more steps in the environment to verify your answer.
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

    def get_instruction_set(self, frontier_node_list, room_node_list, object_node_list, Answer_options):

      instructions = f'''
        Your answer should strictly follow the below JSON format. Note that you should
        choose only one of the two types of available steps each query. The options are 
        "Goto_frontier_node_step" and "Goto_object_node_step".
        Before you populate any of the sections below, be sure to NOT wrap any particular word or phrase
        in double quotes. This will cause JSON parsing to not work correctly, and this is needed of the 
        JSON string output. You must provide content for each entry in the JSON below; leave nothing empty or blank.

        {{
        "steps": [
            {{
            {{"type": "Goto_frontier_node_step"}},
            {{"explanation_frontier": "a string explaning your choice of frontier if you "}},
            {{"frontier_id": "select one from: {', '.join(member.value for member in frontier_node_list)}"}}
            }},
            {{
            {{"type": "Goto_object_node_step"}},
            {{"explanation_room": "Explain very briefly reasoning for selecting this room."}},
            {{"explanation_obj": "Explain very briefly reasoning for selecting this object in the selected room."}},
            {{"room_id": "select one from: {', '.join(member.name for member in room_node_list)}"}},
            {{"room_name": "Refer to the the scene graph to output the room_name corresponding to the selected room_id."}},
            {{"object_id": "Only select objects within the room chosen. Select one from: {', '.join(member.name for member in object_node_list)}. Object IDs will always look like 'object_<NUM>' where NUM is the object number."}},
            {{"object_name": "Refer to the the scene graph to output the object_name corresponding to the selected object_id. This will always be a descriptor."}}
            }}
        ]
        }},
        {{
        "answer": {{
            {{"explanation_ans": "Select the correct answer from the options."}},
            {{"answer": "select one from: {', '.join(member.value for member in Answer_options)}"}},
            {{"value": "select one from: {', '.join(member.name for member in Answer_options)}"}},
            {{"explanation_conf": "Explain the reasoning behind the confidence level of your answer."}},
            {{"confidence_level": "Choose a number ranging from '0.0' to '1.0'. You must return a number in this range."}},
            {{"is_confident": "Select the string 'true' or 'false'."}}
        }}
        }},
        {{"image_description": "Describe the CURRENT IMAGE. Pay special attention to features that can help answer the question or select future actions."}},
        {{"scene_graph_description": "Describe the SCENE GRAPH. Pay special attention to features that can help answer the question or select future actions."}},
        {{"question_type": "Use this to describe the type of question you are being asked. Can be one of 'Identification', 'Counting', 'Existence', 'State', 'Location'."}}
        '''
      
      return instructions

    def get_current_state_prompt(self, scene_graph, agent_state):
        # TODO(saumya): Include history
        prompt = f"At t = {self.t}: \n \
            CURRENT AGENT STATE: {agent_state}. \n \
            SCENE GRAPH: {scene_graph}. \n"

        if self._add_history:
            prompt += f"HISTORY: {self._history}"

        return prompt

    def update_history(self, agent_state, step, answer, target_pose, question_type):
        if step['type'] == 'Goto_object_node_step':
            action = f"Goto object_id:{step['object_id']} object name: {step['object_name']}"
        elif step['type'] == 'Goto_frontier_node_step':
            action = f"Goto frontier_id: {step['frontier_id']}"
        else:
            action = f"Answer: {answer['value']}: {answer['answer']}.  Confident: {answer['is_confident']}, Confidence level:{float(answer['confidence_level'])}"
        
        last_step = f'''
            The following information should serve only as HISTORY:
            Agent state(t={self.t}): {agent_state},
            Action(t={self.t}): {action},
            Answer(t={self.t}): {answer['answer']} {answer['value']},
            Question Type: {question_type}
        '''
        self._history += last_step

    def get_llama4_output(self, current_state_prompt):

        frontier_node_list, room_node_list, region_node_list, object_node_list, Answer_options = self.get_actions()
        
        output_instructions = self.get_instruction_set(frontier_node_list, room_node_list, object_node_list, Answer_options)

        messages=[
            {"role": "system", "content": f"AGENT ROLE: {self.agent_role_prompt}"},
            {"role": "system", "content": f"QUESTION: {self._question}"},
            {"role": "user", "content": f"CURRENT STATE: {current_state_prompt}."},
            {"role": "user", "content": f"INSTRUCTIONS: {output_instructions}"}
        ]

        if self._use_image:
            base64_image = encode_image(get_latest_image(self._output_path))
            messages.append(
                { 
                    "role": "user",
                    "content": [
                        {
                        "type": "text",
                        "text": "CURRENT IMAGE: This image represents the current view of the agent. Use this as additional information to answer the question."
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                })

        succ=False
        while not succ:
            try:
                start = time.time()
                completion = client.chat.completions.create(
                    model="meta-llama/" + self._vlm_type,
                    messages=messages
                )
                plan = completion.choices[0].message.content

                try:
                    json_text = re.search(r'\{.*\}', plan, re.DOTALL).group(0)
                    parsed = json.loads(json_text)
                    break
                except Exception as e:
                    print(f"Parsing error: {e}! Retrying...")
                    time.sleep(1)

                # Validate with Pydantic
                # schema = create_planner_response(frontier_node_list, room_node_list, region_node_list, object_node_list, Answer_options)

                # try:
                #     validated = schema(**parsed)
                #     print("Valid plan parsed")
                #     break
                # except ValidationError as e:
                #     print("Validation failed:", e)
            except Exception as e:
                print(f"An error occurred: {e}. Sleeping for 60s")
                time.sleep(60)

        if len(parsed['steps']) > 0:
            step = parsed['steps'][0]
        else:
            step = None

        if self._use_image:
            img_desc = parsed['image_description']
        else:
            img_desc = ' '

        return step, parsed['answer'], img_desc, parsed['scene_graph_description'], parsed['question_type']

    def get_next_action(self):
        # self.sg_sim.update()

        agent_state = self.sg_sim.get_current_semantic_state_str()
        current_state_prompt = self.get_current_state_prompt(self.sg_sim.scene_graph_str, agent_state)

        sg_desc=''
        step, answer, img_desc, sg_desc, question_type = self.get_llama4_output(current_state_prompt)

        if answer['is_confident'].lower() == 'false':
            is_confident = False
        else:
            is_confident = True

        # Saving outputs to file
        self._outputs_to_save.append(f'At t={self._t}: \n \
                                        Agent state: {agent_state} \n \
                                        LLM output: {step}. \n \
                                        Answer: {answer} \n \
                                        Image desc: {img_desc} \n \
                                        Scene graph desc: {sg_desc} \n \n')
        self.full_plan = ' '.join(self._outputs_to_save)
        with open(self._output_path / "llm_outputs.json", "w") as text_file:
            text_file.write(self.full_plan)

        print(f'At t={self._t}: \n {step} \n {answer}')

        if step is None:
            return None, None, is_confident, float(answer['confidence_level']), answer['value']

        if step['type']== 'Goto_object_node_step':
            target_pose = self.sg_sim.get_position_from_id(step['object_id'])
            target_id = step['object_id']
        else:
            target_pose = self.sg_sim.get_position_from_id(step['frontier_id'])
            target_id = step['frontier_id']

        if self._add_history:
            self.update_history(agent_state, step, answer, target_pose, question_type)

        self._t += 1
        return target_pose, target_id, is_confident, float(answer['confidence_level']), answer['value']
