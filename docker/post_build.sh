apt-get update \
    && apt-get install -y --no-install-recommends \
    libjpeg-dev libglm-dev libgl1-mesa-glx \
    libegl1-mesa-dev mesa-utils xorg-dev freeglut3-dev \
    && rm -rf /var/lib/apt/lists/*

cd /root

source activate grapheqa \
    && git clone --branch stable https://github.com/facebookresearch/habitat-sim.git

cd /root/habitat-sim

source activate grapheqa \
    && export CMAKE_ARGS="-DCMAKE_POLICY_VERSION_MINIMUM=3.5" \
    && python setup.py install --headless --bullet