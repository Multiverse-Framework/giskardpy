import numpy as np

from giskardpy.configs.robot_interface_config import RobotInterfaceConfig
from giskardpy.configs.world_config import WorldConfig
from giskardpy.my_types import Derivatives


class WorldWithUr5eConfig(WorldConfig):

    def __init__(self,
                 map_name: str = 'map',
                 localization_joint_name: str = 'localization'):
        super().__init__()
        self.map_name = map_name
        self.localization_joint_name = localization_joint_name

    def setup(self):
        self.set_default_limits({Derivatives.velocity: 0.5,
                                 Derivatives.acceleration: np.inf,
                                 Derivatives.jerk: 15})
        self.add_robot_from_parameter_server()


class Ur5eJointTrajInterfaceConfig(RobotInterfaceConfig):
    map_name: str

    def __init__(self,
                 map_name: str = 'map',
                 localization_joint_name: str = 'localization'):
        self.map_name = map_name
        self.localization_joint_name = localization_joint_name

    def setup(self):
        self.sync_joint_state_topic('/world/ur5e/joint_states')
        self.add_follow_joint_trajectory_server(namespace='/world/ur5e/joint_trajectory_controller/follow_joint_trajectory',
                                                state_topic='/world/ur5e/joint_trajectory_controller/state',
                                                fill_velocity_values=False)
