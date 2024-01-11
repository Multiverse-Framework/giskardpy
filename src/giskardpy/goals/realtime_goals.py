from __future__ import division

from typing import Optional, List

import rospy
from geometry_msgs.msg import Vector3Stamped, PointStamped

from giskardpy.monitors.monitors import ExpressionMonitor
from giskardpy.tasks.task import WEIGHT_BELOW_CA
from giskardpy.goals.pointing import Pointing
import giskardpy.casadi_wrapper as cas
from giskardpy.utils.expression_definition_utils import transform_msg


class RealTimePointing(Pointing):

    def __init__(self,
                 tip_link: str,
                 root_link: str,
                 tip_group: Optional[str] = None,
                 root_group: Optional[str] = None,
                 pointing_axis: Vector3Stamped = None,
                 max_velocity: float = 0.3,
                 weight: float = WEIGHT_BELOW_CA,
                 start_condition: cas.Expression = cas.TrueSymbol,
                 hold_condition: cas.Expression = cas.TrueSymbol,
                 end_condition: cas.Expression = cas.TrueSymbol
                 ):
        initial_goal = PointStamped()
        initial_goal.header.frame_id = 'base_footprint'
        initial_goal.point.x = 1
        initial_goal.point.z = 1
        super().__init__(tip_link=tip_link,
                         goal_point=initial_goal,
                         root_link=root_link,
                         pointing_axis=pointing_axis)
        self.sub = rospy.Subscriber('muh', PointStamped, self.cb)

    def cb(self, data: PointStamped):
        data = transform_msg(self.root, data)
        self.root_P_goal_point = data

