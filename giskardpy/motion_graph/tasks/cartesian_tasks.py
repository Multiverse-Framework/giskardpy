from typing import Optional

from data_types.data_types import Derivatives
from giskardpy import casadi_wrapper as cas
from giskardpy.data_types.data_types import PrefixName, ColorRGBA
from giskardpy.god_map import god_map
from giskardpy.motion_graph.monitors.cartesian_monitors import PositionReached, OrientationReached
from giskardpy.motion_graph.tasks.task import Task, WEIGHT_ABOVE_CA
from symbol_manager import symbol_manager


class CartesianPosition(Task):
    default_reference_velocity = 0.2

    def __init__(self,
                 root_link: PrefixName,
                 tip_link: PrefixName,
                 goal_point: cas.Point3,
                 threshold: float = 0.01,
                 reference_velocity: Optional[float] = None,
                 weight: float = WEIGHT_ABOVE_CA,
                 absolute: bool = False,
                 name: Optional[str] = None,
                 plot: bool = True):
        """
        See CartesianPose.
        """
        self.root_link = root_link
        self.tip_link = tip_link
        if name is None:
            name = f'{self.__class__.__name__}/{self.root_link}/{self.tip_link}'
        super().__init__(name=name, plot=plot)
        if reference_velocity is None:
            reference_velocity = self.default_reference_velocity
        self.reference_velocity = reference_velocity
        self.weight = weight
        if absolute:
            root_P_goal = god_map.world.transform(self.root_link, goal_point)
        else:
            root_T_x = god_map.world.compose_fk_expression(self.root_link, goal_point.reference_frame)
            root_P_goal = root_T_x.dot(goal_point)
            root_P_goal = self.update_expression_on_starting(root_P_goal)

        r_P_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_position()
        self.add_point_goal_constraints(frame_P_goal=root_P_goal,
                                        frame_P_current=r_P_c,
                                        reference_velocity=self.reference_velocity,
                                        weight=self.weight)
        god_map.debug_expression_manager.add_debug_expression(f'{self.name}/target', root_P_goal.y,
                                                              color=ColorRGBA(r=0.0, g=0.0, b=1.0, a=1.0),
                                                              derivative=Derivatives.position,
                                                              derivatives_to_plot=[Derivatives.position])

        cap = self.reference_velocity * god_map.qp_controller.sample_period * (
                god_map.qp_controller.prediction_horizon - 2)
        god_map.debug_expression_manager.add_debug_expression(f'{self.name}/upper_cap', root_P_goal.y + cap,
                                                              derivatives_to_plot=[
                                                                  Derivatives.position,
                                                              ])
        god_map.debug_expression_manager.add_debug_expression(f'{self.name}/lower_cap', root_P_goal.y - cap,
                                                              derivatives_to_plot=[
                                                                  Derivatives.position,
                                                              ])
        god_map.debug_expression_manager.add_debug_expression(f'{self.name}/current', r_P_c.y,
                                                              color=ColorRGBA(r=1.0, g=0.0, b=0.0, a=1.0),
                                                              derivative=Derivatives.position,
                                                              derivatives_to_plot=Derivatives.range(
                                                                  Derivatives.position,
                                                                  Derivatives.jerk)
                                                              )

        distance_to_goal = cas.euclidean_distance(root_P_goal, r_P_c)
        self.expression = cas.less(distance_to_goal, threshold)


class CartesianOrientation(Task):
    default_reference_velocity = 0.5

    def __init__(self,
                 root_link: PrefixName,
                 tip_link: PrefixName,
                 goal_orientation: cas.RotationMatrix,
                 threshold: float = 0.01,
                 reference_velocity: Optional[float] = None,
                 weight: float = WEIGHT_ABOVE_CA,
                 name: Optional[str] = None,
                 absolute: bool = False,
                 point_of_debug_matrix: Optional[cas.Point3] = None):
        """
        See CartesianPose.
        """
        self.root_link = root_link
        self.tip_link = tip_link
        if name is None:
            name = f'{self.__class__.__name__}/{self.root_link}/{self.tip_link}'
        super().__init__(name=name)
        if reference_velocity is None:
            reference_velocity = self.default_reference_velocity
        self.reference_velocity = reference_velocity
        self.weight = weight

        if absolute:
            root_R_goal = god_map.world.transform(self.root_link, goal_orientation)
        else:
            root_T_x = god_map.world.compose_fk_expression(self.root_link, goal_orientation.reference_frame)
            root_R_goal = root_T_x.dot(goal_orientation)
            root_R_goal = self.update_expression_on_starting(root_R_goal)

        r_T_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link)
        r_R_c = r_T_c.to_rotation()
        c_R_r_eval = god_map.world.compose_fk_evaluated_expression(self.tip_link, self.root_link).to_rotation()

        self.add_rotation_goal_constraints(frame_R_current=r_R_c,
                                           frame_R_goal=root_R_goal,
                                           current_R_frame_eval=c_R_r_eval,
                                           reference_velocity=self.reference_velocity,
                                           weight=self.weight)
        if point_of_debug_matrix is None:
            point = r_T_c.to_position()
        else:
            if absolute:
                point = point_of_debug_matrix
            else:
                root_T_x = god_map.world.compose_fk_expression(self.root_link, point_of_debug_matrix.reference_frame)
                point = root_T_x.dot(point_of_debug_matrix)
                point = self.update_expression_on_starting(point)
        debug_trans_matrix = cas.TransMatrix.from_point_rotation_matrix(point=point,
                                                                        rotation_matrix=root_R_goal)
        debug_current_trans_matrix = cas.TransMatrix.from_point_rotation_matrix(point=r_T_c.to_position(),
                                                                                rotation_matrix=r_R_c)
        # god_map.debug_expression_manager.add_debug_expression(f'{self.name}/goal_orientation', debug_trans_matrix)
        # god_map.debug_expression_manager.add_debug_expression(f'{self.name}/current_orientation',
        #                                                       debug_current_trans_matrix)

        rotation_error = cas.rotational_error(r_R_c, root_R_goal)
        self.expression = cas.less(cas.abs(rotation_error), threshold)


class CartesianPoseAsTask(Task):
    def __init__(self,
                 root_link: PrefixName,
                 tip_link: PrefixName,
                 goal_pose: cas.TransMatrix,
                 reference_linear_velocity: Optional[float] = None,
                 reference_angular_velocity: Optional[float] = None,
                 threshold: float = 0.01,
                 name: Optional[str] = None,
                 absolute: bool = False,
                 weight=WEIGHT_ABOVE_CA):
        """
        This goal will use the kinematic chain between root and tip link to move tip link into the goal pose.
        The max velocities enforce a strict limit, but require a lot of additional constraints, thus making the
        system noticeably slower.
        The reference velocities don't enforce a strict limit, but also don't require any additional constraints.
        :param root_link: name of the root link of the kin chain
        :param tip_link: name of the tip link of the kin chain
        :param goal_pose: the goal pose
        :param absolute: if False, the goal is updated when start_condition turns True.
        :param reference_linear_velocity: m/s
        :param reference_angular_velocity: rad/s
        :param weight: default WEIGHT_ABOVE_CA
        """
        self.root_link = root_link
        self.tip_link = tip_link
        if name is None:
            name = f'{self.__class__.__name__}/{self.root_link}/{self.tip_link}'
        super().__init__(name=name)
        if reference_linear_velocity is None:
            reference_linear_velocity = CartesianOrientation.default_reference_velocity
        self.reference_linear_velocity = reference_linear_velocity
        if reference_angular_velocity is None:
            reference_angular_velocity = CartesianOrientation.default_reference_velocity
        self.reference_angular_velocity = reference_angular_velocity

        self.weight = weight
        goal_orientation = goal_pose.to_rotation()
        goal_point = goal_pose.to_position()

        if absolute:
            root_P_goal = god_map.world.transform(self.root_link, goal_point)
            root_R_goal = god_map.world.transform(self.root_link, goal_orientation)
        else:
            root_T_x = god_map.world.compose_fk_expression(self.root_link, goal_point.reference_frame)
            root_P_goal = root_T_x.dot(goal_point)
            root_P_goal = self.update_expression_on_starting(root_P_goal)
            root_R_goal = root_T_x.dot(goal_orientation)
            root_R_goal = self.update_expression_on_starting(root_R_goal)

        r_P_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_position()
        self.add_point_goal_constraints(frame_P_goal=root_P_goal,
                                        frame_P_current=r_P_c,
                                        reference_velocity=self.reference_linear_velocity,
                                        weight=self.weight)
        god_map.debug_expression_manager.add_debug_expression(f'{self.name}/current_point', r_P_c,
                                                              color=ColorRGBA(r=1.0, g=0.0, b=0.0, a=1.0))
        god_map.debug_expression_manager.add_debug_expression(f'{self.name}/goal_point', root_P_goal,
                                                              color=ColorRGBA(r=0.0, g=0.0, b=1.0, a=1.0))

        distance_to_goal = cas.euclidean_distance(root_P_goal, r_P_c)

        r_T_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link)
        r_R_c = r_T_c.to_rotation()
        c_R_r_eval = god_map.world.compose_fk_evaluated_expression(self.tip_link, self.root_link).to_rotation()

        self.add_rotation_goal_constraints(frame_R_current=r_R_c,
                                           frame_R_goal=root_R_goal,
                                           current_R_frame_eval=c_R_r_eval,
                                           reference_velocity=self.reference_angular_velocity,
                                           weight=self.weight)
        debug_trans_matrix = cas.TransMatrix.from_point_rotation_matrix(point=goal_point,
                                                                        rotation_matrix=root_R_goal)
        debug_current_trans_matrix = cas.TransMatrix.from_point_rotation_matrix(point=r_T_c.to_position(),
                                                                                rotation_matrix=r_R_c)
        # god_map.debug_expression_manager.add_debug_expression(f'{self.name}/goal_orientation', debug_trans_matrix)
        # god_map.debug_expression_manager.add_debug_expression(f'{self.name}/current_orientation',
        #                                                       debug_current_trans_matrix)

        rotation_error = cas.rotational_error(r_R_c, root_R_goal)
        self.expression = cas.logic_and(cas.less(cas.abs(rotation_error), threshold),
                                        cas.less(distance_to_goal, threshold))


class CartesianPositionVelocityLimit(Task):
    def __init__(self,
                 root_link: PrefixName,
                 tip_link: PrefixName,
                 name: str,
                 max_linear_velocity: float = 0.2,
                 weight: float = WEIGHT_ABOVE_CA):
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
        super().__init__(name=name)
        r_P_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_position()
        self.add_translational_velocity_limit(frame_P_current=r_P_c,
                                              max_velocity=max_linear_velocity,
                                              weight=weight)


class CartesianPositionVelocityGoal(Task):
    def __init__(self,
                 root_link: PrefixName,
                 tip_link: PrefixName,
                 name: str,
                 x_vel: float,
                 y_vel: float,
                 z_vel: float,
                 weight: float = WEIGHT_ABOVE_CA):
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
        super().__init__(name=name)
        r_P_c = god_map.world.compose_fk_expression(self.root_link, self.tip_link).to_position()
        god_map.debug_expression_manager.add_debug_expression(f'{self.name}/target',
                                                              cas.Expression(y_vel),
                                                              derivative=Derivatives.velocity,
                                                              derivatives_to_plot=[
                                                                  # Derivatives.position,
                                                                  Derivatives.velocity
                                                              ])
        god_map.debug_expression_manager.add_debug_expression(f'{self.name}/current', r_P_c.y,
                                                              derivative=Derivatives.position,
                                                              derivatives_to_plot=Derivatives.range(
                                                                  Derivatives.position,
                                                                  Derivatives.jerk)
                                                              )
        self.add_velocity_eq_constraint_vector(velocity_goals=cas.Expression([x_vel, y_vel, z_vel]),
                                               task_expression=r_P_c,
                                               reference_velocities=[
                                                   max(CartesianPosition.default_reference_velocity, abs(x_vel)),
                                                   max(CartesianPosition.default_reference_velocity, abs(y_vel)),
                                                   max(CartesianPosition.default_reference_velocity, abs(z_vel))
                                               ],
                                               names=[
                                                   f'{name}/x',
                                                   f'{name}/y',
                                                   f'{name}/z',
                                               ],
                                               weights=[weight] * 3)
