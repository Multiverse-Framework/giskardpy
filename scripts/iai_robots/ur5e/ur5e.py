#!/usr/bin/env python
import rospy

from giskardpy.configs.giskard import Giskard
from giskardpy.configs.iai_robots.ur5e import WorldWithUr5eConfig, Ur5eJointTrajInterfaceConfig

if __name__ == '__main__':
    rospy.init_node('giskard')
    giskard = Giskard(world_config=WorldWithUr5eConfig(),
                      robot_interface_config=Ur5eJointTrajInterfaceConfig())
    giskard.live()
