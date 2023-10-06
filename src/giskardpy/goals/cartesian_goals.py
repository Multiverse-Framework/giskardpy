from __future__ import division

from typing import Optional

import numpy as np
from geometry_msgs.msg import PointStamped, PoseStamped, QuaternionStamped
from geometry_msgs.msg import Vector3Stamped
from tf.transformations import rotation_from_matrix

from giskardpy import casadi_wrapper as cas
from giskardpy.goals.goal import Goal
from giskardpy.goals.monitors.monitors import Monitor
from giskardpy.goals.tasks.task import WEIGHT_BELOW_CA, WEIGHT_ABOVE_CA, WEIGHT_COLLISION_AVOIDANCE, Task
from giskardpy.god_map import god_map
from giskardpy.model.joints import DiffDrive, OmniDrivePR22
from giskardpy.my_types import Derivatives
from giskardpy.symbol_manager import symbol_manager
from giskardpy.utils import logging
from giskardpy.utils.tfwrapper import normalize
from giskardpy.utils.utils import split_pose_stamped


class CartesianPosition(Goal):
    default_reference_velocity = 0.2

    def __init__(self, root_link: str, tip_link: str, goal_point: PointStamped,
                 root_group: Optional[str] = None,
                 tip_group: Optional[str] = None,
                 reference_velocity: Optional[float] = None,
                 weight: float = WEIGHT_ABOVE_CA):
        """
        See CartesianPose.
        """
        self.root_link = god_map.world.search_for_link_name(root_link, root_group)
        self.tip_link = god_map.world.search_for_link_name(tip_link, tip_group)
        super().__init__()
        if reference_velocity is None:
            reference_velocity = self.default_reference_velocity
        self.goal_point = self.transform_msg(self.root_link, goal_point)
        self.reference_velocity = reference_velocity
        self.weight = weight

        r_P_g = cas.Point3(self.goal_point)
        r_P_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_position()
        task = Task(name='position goal')
        task.add_point_goal_constraints(frame_P_goal=r_P_g,
                                        frame_P_current=r_P_c,
                                        reference_velocity=self.reference_velocity,
                                        weight=self.weight)
        self.add_task(task)

    def __str__(self):
        s = super().__str__()
        return f'{s}/{self.root_link}/{self.tip_link}'


class CartesianOrientation(Goal):
    default_reference_velocity = 0.5

    def __init__(self,
                 root_link: str,
                 tip_link: str,
                 goal_orientation: QuaternionStamped,
                 root_group: Optional[str] = None,
                 tip_group: Optional[str] = None,
                 reference_velocity: Optional[float] = None,
                 weight: float = WEIGHT_ABOVE_CA):
        """
        See CartesianPose.
        """
        self.root_link = god_map.world.search_for_link_name(root_link, root_group)
        self.tip_link = god_map.world.search_for_link_name(tip_link, tip_group)
        super().__init__()
        if reference_velocity is None:
            reference_velocity = self.default_reference_velocity
        self.goal_orientation = self.transform_msg(self.root_link, goal_orientation)
        self.reference_velocity = reference_velocity
        self.weight = weight

        r_R_g = cas.RotationMatrix(self.goal_orientation)
        r_R_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_rotation()
        c_R_r_eval = god_map.world.compose_fk_evaluated_expression(self.tip_link, self.root_link).to_rotation()

        task = Task(name='rotation goal')
        task.add_rotation_goal_constraints(frame_R_current=r_R_c,
                                           frame_R_goal=r_R_g,
                                           current_R_frame_eval=c_R_r_eval,
                                           reference_velocity=self.reference_velocity,
                                           weight=self.weight)
        self.add_task(task)

    def __str__(self):
        s = super().__str__()
        return f'{s}/{self.root_link}/{self.tip_link}'


class CartesianPositionStraight(Goal):
    def __init__(self,
                 root_link: str,
                 tip_link: str,
                 goal_point: PointStamped,
                 root_group: Optional[str] = None,
                 tip_group: Optional[str] = None,
                 reference_velocity: Optional[float] = None,
                 weight: float = WEIGHT_ABOVE_CA):
        """
        Same as CartesianPosition, but tries to move the tip_link in a straight line to the goal_point.
        """
        super().__init__()
        if reference_velocity is None:
            reference_velocity = 0.2
        self.reference_velocity = reference_velocity
        self.weight = weight
        self.root_link = god_map.world.search_for_link_name(root_link, root_group)
        self.tip_link = god_map.world.search_for_link_name(tip_link, tip_group)
        self.goal_point = self.transform_msg(self.root_link, goal_point)

        root_P_goal = cas.Point3(self.goal_point)
        root_P_tip = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_position()
        t_T_r = god_map.world.compose_fk_expression(self.tip_link, self.root_link)
        tip_P_goal = t_T_r.dot(root_P_goal)

        # Create rotation matrix, which rotates the tip link frame
        # such that its x-axis shows towards the goal position.
        # The goal frame is called 'a'.
        # Thus, the rotation matrix is called t_R_a.
        tip_V_error = cas.Vector3(tip_P_goal)
        trans_error = tip_V_error.norm()
        # x-axis
        tip_V_intermediate_error = cas.save_division(tip_V_error, trans_error)
        # y- and z-axis
        tip_V_intermediate_y = cas.Vector3(np.random.random((3,)))
        tip_V_intermediate_y.scale(1)
        y = tip_V_intermediate_error.cross(tip_V_intermediate_y)
        z = tip_V_intermediate_error.cross(y)
        t_R_a = cas.RotationMatrix.from_vectors(x=tip_V_intermediate_error, y=-z, z=y)

        # Apply rotation matrix on the fk of the tip link
        a_T_t = t_R_a.inverse().dot(
            god_map.world.compose_fk_evaluated_expression(self.tip_link, self.root_link)).dot(
            god_map.world.compose_fk_expression(self.root_link, self.tip_link))
        expr_p = a_T_t.to_position()
        dist = cas.norm(root_P_goal - root_P_tip)

        task = Task(name='position straight')
        task.add_equality_constraint_vector(reference_velocities=[self.reference_velocity] * 3,
                                            equality_bounds=[dist, 0, 0],
                                            weights=[WEIGHT_ABOVE_CA, WEIGHT_ABOVE_CA * 2, WEIGHT_ABOVE_CA * 2],
                                            task_expression=expr_p[:3],
                                            names=['line/x',
                                                   'line/y',
                                                   'line/z'])

        self.add_task(task)

    def __str__(self):
        s = super().__str__()
        return f'{s}/{self.root_link}/{self.tip_link}'


class CartesianPose(Goal):
    def __init__(self, root_link: str, tip_link: str, goal_pose: PoseStamped,
                 root_group: Optional[str] = None,
                 tip_group: Optional[str] = None,
                 reference_linear_velocity: Optional[float] = None,
                 reference_angular_velocity: Optional[float] = None,
                 weight=WEIGHT_ABOVE_CA):
        """
        This goal will use the kinematic chain between root and tip link to move tip link into the goal pose.
        The max velocities enforce a strict limit, but require a lot of additional constraints, thus making the
        system noticeably slower.
        The reference velocities don't enforce a strict limit, but also don't require any additional constraints.
        :param root_link: name of the root link of the kin chain
        :param tip_link: name of the tip link of the kin chain
        :param goal_pose: the goal pose
        :param root_group: a group name, where to search for root_link, only required to avoid name conflicts
        :param tip_group: a group name, where to search for tip_link, only required to avoid name conflicts
        :param max_linear_velocity: m/s
        :param max_angular_velocity: rad/s
        :param reference_linear_velocity: m/s
        :param reference_angular_velocity: rad/s
        :param weight: default WEIGHT_ABOVE_CA
        """
        self.root_link = god_map.world.search_for_link_name(root_link, root_group)
        self.tip_link = god_map.world.search_for_link_name(tip_link, tip_group)
        super().__init__()
        if reference_linear_velocity is None:
            reference_linear_velocity = CartesianPosition.default_reference_velocity
        if reference_angular_velocity is None:
            reference_angular_velocity = CartesianOrientation.default_reference_velocity
        self.weight = weight
        self.goal_pose = self.transform_msg(self.root_link, goal_pose)

        goal_point, goal_quaternion = split_pose_stamped(goal_pose)

        position_goal = CartesianPosition(root_link=root_link,
                                          tip_link=tip_link,
                                          goal_point=goal_point,
                                          root_group=root_group,
                                          tip_group=tip_group,
                                          reference_velocity=reference_linear_velocity,
                                          weight=self.weight)
        self.add_tasks(position_goal.tasks)

        orientation_goal = CartesianOrientation(root_link=root_link,
                                                tip_link=tip_link,
                                                goal_orientation=goal_quaternion,
                                                root_group=root_group,
                                                tip_group=tip_group,
                                                reference_velocity=reference_angular_velocity,
                                                weight=self.weight)
        self.add_tasks(orientation_goal.tasks)

    def __str__(self):
        s = super().__str__()
        return f'{s}/{self.root_link}/{self.tip_link}'


class DiffDriveBaseGoal(Goal):

    def __init__(self, root_link: str, tip_link: str, goal_pose: PoseStamped, max_linear_velocity: float = 0.1,
                 max_angular_velocity: float = 0.5, weight: float = WEIGHT_ABOVE_CA, pointing_axis=None,
                 root_group: Optional[str] = None, tip_group: Optional[str] = None,
                 always_forward: bool = False):
        """
        Like a CartesianPose, but specifically for differential drives. It will achieve the goal in 3 phases.
        1. orient towards goal.
        2. drive to goal point.
        3. reach goal orientation.
        :param root_link: root link of the kinematic chain. typically map
        :param tip_link: tip link of the kinematic chain. typically base_footprint or similar
        :param goal_pose:
        :param max_linear_velocity:
        :param max_angular_velocity:
        :param weight:
        :param pointing_axis: the forward direction. default is x-axis
        :param always_forward: if false, it will drive backwards, if it requires less rotation.
        """
        super().__init__()
        self.always_forward = always_forward
        self.max_linear_velocity = max_linear_velocity
        self.max_angular_velocity = max_angular_velocity
        if pointing_axis is None:
            pointing_axis = Vector3Stamped()
            pointing_axis.header.frame_id = tip_link
            pointing_axis.vector.x = 1
        self.weight = weight
        self.map = god_map.world.search_for_link_name(root_link, root_group)
        self.base_footprint = god_map.world.search_for_link_name(tip_link, tip_group)
        self.goal_pose = self.transform_msg(self.map, goal_pose)
        self.goal_pose.pose.position.z = 0
        diff_drive_joints = [v for k, v in god_map.world.joints.items() if isinstance(v, DiffDrive)]
        assert len(diff_drive_joints) == 1
        self.joint: DiffDrive = diff_drive_joints[0]
        self.odom = self.joint.parent_link_name

        if pointing_axis is not None:
            self.base_footprint_V_pointing_axis = self.transform_msg(self.base_footprint, pointing_axis)
            self.base_footprint_V_pointing_axis.vector = normalize(self.base_footprint_V_pointing_axis.vector)
        else:
            self.base_footprint_V_pointing_axis = Vector3Stamped()
            self.base_footprint_V_pointing_axis.header.frame_id = self.base_footprint
            self.base_footprint_V_pointing_axis.vector.z = 1

    def make_constraints(self):
        map_T_base_current = cas.TransMatrix(god_map.world.compute_fk_np(self.map, self.base_footprint))
        map_T_odom_current = god_map.world.compute_fk_np(self.map, self.odom)
        map_odom_angle, _, _ = rotation_from_matrix(map_T_odom_current)
        map_R_base_current = map_T_base_current.to_rotation()
        axis_start, angle_start = map_R_base_current.to_axis_angle()
        angle_start = cas.if_greater_zero(axis_start[2], angle_start, -angle_start)

        map_T_base_footprint = god_map.world.compose_fk_expression(self.map, self.base_footprint)
        map_P_base_footprint = map_T_base_footprint.to_position()
        # map_R_base_footprint = map_T_base_footprint.to_rotation()
        map_T_base_footprint_goal = cas.TransMatrix(self.goal_pose)
        map_P_base_footprint_goal = map_T_base_footprint_goal.to_position()
        map_R_base_footprint_goal = map_T_base_footprint_goal.to_rotation()

        map_V_goal_x = map_P_base_footprint_goal - map_P_base_footprint
        distance = map_V_goal_x.norm()

        # axis, map_current_angle = map_R_base_footprint.to_axis_angle()
        # map_current_angle = cas.if_greater_zero(axis.z, map_current_angle, -map_current_angle)
        odom_current_angle = self.joint.yacas.get_symbol(Derivatives.position)
        map_current_angle = map_odom_angle + odom_current_angle

        axis2, map_goal_angle2 = map_R_base_footprint_goal.to_axis_angle()
        map_goal_angle2 = cas.if_greater_zero(axis2.z, map_goal_angle2, -map_goal_angle2)
        final_rotation_error = cas.shortest_angular_distance(map_current_angle, map_goal_angle2)

        map_R_goal = cas.RotationMatrix.from_vectors(x=map_V_goal_x, y=None, z=cas.Vector3((0, 0, 1)))

        map_goal_angle_direction_f = map_R_goal.to_angle(lambda axis: axis[2])

        map_goal_angle_direction_b = cas.if_less_eq(map_goal_angle_direction_f, 0,
                                                    if_result=map_goal_angle_direction_f + np.pi,
                                                    else_result=map_goal_angle_direction_f - np.pi)

        middle_angle = cas.normalize_angle(
            map_goal_angle2 + cas.shortest_angular_distance(map_goal_angle2, angle_start) / 2)

        middle_angle = symbol_manager.evaluate_expr(middle_angle)
        a = symbol_manager.evaluate_expr(cas.shortest_angular_distance(map_goal_angle_direction_f, middle_angle))
        b = symbol_manager.evaluate_expr(cas.shortest_angular_distance(map_goal_angle_direction_b, middle_angle))
        eps = 0.01
        if self.always_forward:
            map_goal_angle1 = map_goal_angle_direction_f
        else:
            map_goal_angle1 = cas.if_less_eq(cas.abs(a) - cas.abs(b), 0.03,
                                             if_result=map_goal_angle_direction_f,
                                             else_result=map_goal_angle_direction_b)
        rotate_to_goal_error = cas.shortest_angular_distance(map_current_angle, map_goal_angle1)

        weight_final_rotation = cas.if_else(cas.logic_and(cas.less_equal(cas.abs(distance), eps * 2),
                                                          cas.greater_equal(cas.abs(final_rotation_error), 0)),
                                            self.weight,
                                            0)
        weight_rotate_to_goal = cas.if_else(cas.logic_and(cas.greater_equal(cas.abs(rotate_to_goal_error), eps),
                                                          cas.greater_equal(cas.abs(distance), eps),
                                                          cas.less_equal(weight_final_rotation, eps)),
                                            self.weight,
                                            0)
        weight_translation = cas.if_else(cas.logic_and(cas.less_equal(cas.abs(rotate_to_goal_error), eps * 2),
                                                       cas.greater_equal(cas.abs(distance), eps)),
                                         self.weight,
                                         0)

        self.add_equality_constraint(reference_velocity=self.max_angular_velocity,
                                     equality_bound=rotate_to_goal_error,
                                     weight=weight_rotate_to_goal,
                                     task_expression=map_current_angle,
                                     name='/rot1')
        self.add_point_goal_constraints(frame_P_current=map_P_base_footprint,
                                        frame_P_goal=map_P_base_footprint_goal,
                                        reference_velocity=self.max_linear_velocity,
                                        weight=weight_translation)
        self.add_equality_constraint(reference_velocity=self.max_angular_velocity,
                                     equality_bound=final_rotation_error,
                                     weight=weight_final_rotation,
                                     task_expression=map_current_angle,
                                     name='/rot2')

    def __str__(self):
        s = super().__str__()
        return f'{s}/{self.map}/{self.base_footprint}'


class PR2DiffDriveBaseGoal(Goal):

    def __init__(self, goal_pose: PoseStamped, max_linear_velocity: float = 0.1,
                 max_angular_velocity: float = 0.5, weight: float = WEIGHT_ABOVE_CA, pointing_axis=None,
                 root_group: Optional[str] = None, tip_group: Optional[str] = None):
        super().__init__()
        self.max_angular_velocity = max_angular_velocity
        self.max_linear_velocity = max_linear_velocity
        diff_drive_joints = [v for k, v in god_map.world.joints.items() if isinstance(v, OmniDrivePR22)]
        assert len(diff_drive_joints) == 1
        self.joint: OmniDrivePR22 = diff_drive_joints[0]
        self.weight = weight
        self.root_link = self.joint.parent_link_name
        self.tip_link = self.joint.child_link_name
        self.root_T_goal = self.transform_msg(self.root_link, goal_pose)
        self.add_constraints_of_goal(CartesianPose(root_link=self.root_link,
                                                   tip_link=self.tip_link,
                                                   goal_pose=goal_pose,
                                                   reference_linear_velocity=self.max_linear_velocity,
                                                   reference_angular_velocity=self.max_angular_velocity,
                                                   weight=WEIGHT_BELOW_CA))

    def make_constraints(self):
        root_T_tip = god_map.world.compose_fk_expression(self.root_link, self.tip_link)
        root_P_tip = root_T_tip.to_position()

        root_T_goal = cas.TransMatrix(self.root_T_goal)
        root_P_goal = root_T_goal.to_position()

        root_yaw1 = self.joint.yaw1_vel.get_symbol(Derivatives.position)
        root_V_forward = cas.Vector3((cas.cos(root_yaw1), cas.sin(root_yaw1), 0))
        root_V_forward.vis_frame = self.tip_link

        root_V_goal = root_P_goal - root_P_tip
        root_V_goal.scale(1)
        root_V_goal.vis_frame = self.tip_link

        self.add_debug_expr('root_P_goal', root_P_goal)
        self.add_debug_expr('root_V_forward', root_V_forward)
        self.add_debug_expr('root_V_goal', root_V_goal)

        weight = cas.if_greater(cas.norm(root_P_goal - root_P_tip), 0.005, WEIGHT_ABOVE_CA, 0)

        self.add_vector_goal_constraints(frame_V_current=root_V_forward,
                                         frame_V_goal=root_V_goal,
                                         reference_velocity=self.max_angular_velocity,
                                         weight=weight,
                                         name='angle')

    def __str__(self) -> str:
        return super().__str__()


class CartesianPoseStraight(Goal):
    def __init__(self, root_link: str, tip_link: str, goal_pose: PoseStamped,
                 root_group: Optional[str] = None,
                 tip_group: Optional[str] = None,
                 reference_linear_velocity: Optional[float] = None,
                 reference_angular_velocity: Optional[float] = None,
                 weight: float = WEIGHT_ABOVE_CA):
        """
        See CartesianPose. In contrast to it, this goal will try to move tip_link in a straight line.
        """
        self.root_link = root_link
        self.tip_link = tip_link
        super().__init__()
        goal_point, goal_orientation = split_pose_stamped(goal_pose)
        goal = CartesianPositionStraight(root_link=root_link,
                                         root_group=root_group,
                                         tip_link=tip_link,
                                         tip_group=tip_group,
                                         goal_point=goal_point,
                                         reference_velocity=reference_linear_velocity,
                                         weight=weight)
        self.add_tasks(goal.tasks)
        goal = CartesianOrientation(root_link=root_link,
                                    root_group=root_group,
                                    tip_link=tip_link,
                                    tip_group=tip_group,
                                    goal_orientation=goal_orientation,
                                    reference_velocity=reference_angular_velocity,
                                    weight=weight)
        self.add_tasks(goal.tasks)

    def __str__(self):
        s = super().__str__()
        return f'{s}/{self.root_link}/{self.tip_link}'


class TranslationVelocityLimit(Goal):
    def __init__(self, root_link: str, tip_link: str, root_group: Optional[str] = None, tip_group: Optional[str] = None,
                 weight=WEIGHT_ABOVE_CA, max_velocity=0.1, hard=True):
        """
        See CartesianVelocityLimit
        """
        super().__init__()
        self.root_link = god_map.world.search_for_link_name(root_link, root_group)
        self.tip_link = god_map.world.search_for_link_name(tip_link, tip_group)
        self.hard = hard
        self.weight = weight
        self.max_velocity = max_velocity

        r_P_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_position()
        # self.add_debug_expr('limit', -self.max_velocity)
        task = Task(name='limit translation vel')
        if not self.hard:
            task.add_translational_velocity_limit(frame_P_current=r_P_c,
                                                  max_velocity=self.max_velocity,
                                                  weight=self.weight)
        else:
            task.add_translational_velocity_limit(frame_P_current=r_P_c,
                                                  max_velocity=self.max_velocity,
                                                  weight=self.weight,
                                                  max_violation=0)
        self.add_task(task)

    def __str__(self):
        s = super(TranslationVelocityLimit, self).__str__()
        return f'{s}/{self.root_link}/{self.tip_link}'


class RotationVelocityLimit(Goal):
    def __init__(self, root_link: str, tip_link: str, root_group: Optional[str] = None, tip_group: Optional[str] = None,
                 weight=WEIGHT_ABOVE_CA, max_velocity=0.5, hard=True):
        """
        See CartesianVelocityLimit
        """

        super().__init__()
        self.root_link = god_map.world.search_for_link_name(root_link, root_group)
        self.tip_link = god_map.world.search_for_link_name(tip_link, tip_group)
        self.hard = hard

        self.weight = weight
        self.max_velocity = max_velocity

        r_R_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_rotation()

        task = Task(name='rot vel limit')
        if self.hard:
            task.add_rotational_velocity_limit(frame_R_current=r_R_c,
                                               max_velocity=self.max_velocity,
                                               weight=self.weight)
        else:
            task.add_rotational_velocity_limit(frame_R_current=r_R_c,
                                               max_velocity=self.max_velocity,
                                               weight=self.weight,
                                               max_violation=0)
        self.add_task(task)

    def __str__(self):
        s = super().__str__()
        return f'{s}/{self.root_link}/{self.tip_link}'


class CartesianVelocityLimit(Goal):
    def __init__(self,
                 root_link: str,
                 tip_link: str,
                 root_group: Optional[str] = None,
                 tip_group: Optional[str] = None,
                 max_linear_velocity: float = 0.1,
                 max_angular_velocity: float = 0.5,
                 weight: float = WEIGHT_ABOVE_CA,
                 hard: bool = False):
        """
        This goal will use put a strict limit on the Cartesian velocity. This will require a lot of constraints, thus
        slowing down the system noticeably.
        :param root_link: root link of the kinematic chain
        :param tip_link: tip link of the kinematic chain
        :param root_group: if the root_link is not unique, use this to say to which group the link belongs
        :param tip_group: if the tip_link is not unique, use this to say to which group the link belongs
        :param max_linear_velocity: m/s
        :param max_angular_velocity: rad/s
        :param weight: default WEIGHT_ABOVE_CA
        :param hard: Turn this into a hard constraint. This make create unsolvable optimization problems
        """
        self.root_link = root_link
        self.tip_link = tip_link
        super().__init__()
        self.add_constraints_of_goal(TranslationVelocityLimit(root_link=root_link,
                                                              root_group=root_group,
                                                              tip_link=tip_link,
                                                              tip_group=tip_group,
                                                              max_velocity=max_linear_velocity,
                                                              weight=weight,
                                                              hard=hard))
        self.add_constraints_of_goal(RotationVelocityLimit(root_link=root_link,
                                                           root_group=root_group,
                                                           tip_link=tip_link,
                                                           tip_group=tip_group,
                                                           max_velocity=max_angular_velocity,
                                                           weight=weight,
                                                           hard=hard))

    def __str__(self):
        s = super().__str__()
        return f'{s}/{self.root_link}/{self.tip_link}'


class RelativePositionSequence(Goal):
    def __init__(self,
                 goal1: PointStamped,
                 goal2: PointStamped,
                 root_link: str,
                 tip_link: str):
        super().__init__()
        self.root_link = god_map.world.search_for_link_name(root_link)
        self.tip_link = god_map.world.search_for_link_name(tip_link)
        self.root_P_goal1 = self.transform_msg(self.root_link, goal1)
        self.tip_P_goal2 = self.transform_msg(self.tip_link, goal2)
        self.max_velocity = 0.1
        self.weight = WEIGHT_BELOW_CA

        root_P_current = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_position()

        root_P_goal1 = cas.Point3(self.root_P_goal1)
        tip_P_goal2 = cas.Point3(self.tip_P_goal2)
        root_P_goal2 = god_map.world.compose_fk_expression(self.root_link, self.tip_link).dot(tip_P_goal2)

        error1 = cas.euclidean_distance(root_P_goal1, root_P_current)
        error1_monitor = Monitor(name='p1',
                                 crucial=True,
                                 stay_one=True)
        self.add_monitor(error1_monitor)
        error1_monitor.set_expression(cas.less(cas.abs(error1), 0.01))

        error2_monitor = Monitor(name='p2',
                                 crucial=True,
                                 stay_one=True)
        self.add_monitor(error2_monitor)
        root_P_goal2_cached = error1_monitor.substitute_with_on_flip_symbols(root_P_goal2)

        error2 = cas.euclidean_distance(root_P_goal2_cached, root_P_current)
        error2_monitor.set_expression(cas.less(cas.abs(error2), 0.01))

        step1 = Task(name='step1')
        step1.add_to_end_monitor(error1_monitor)
        step1.add_point_goal_constraints(root_P_current, root_P_goal1,
                                         reference_velocity=self.max_velocity,
                                         weight=self.weight)
        self.add_task(step1)

        self.step2 = Task(name='step2')
        self.step2.add_to_start_monitor(error1_monitor)
        self.step2.add_to_end_monitor(error2_monitor)
        self.step2.add_point_goal_constraints(root_P_current, root_P_goal2_cached,
                                              reference_velocity=self.max_velocity,
                                              weight=self.weight)
        self.add_task(self.step2)

    def connect_to_end(self, monitor: Monitor):
        self.step2.add_to_end_monitor(monitor)

    def __str__(self) -> str:
        return super().__str__()
