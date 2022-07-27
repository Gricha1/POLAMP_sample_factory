import gym
import matplotlib.pyplot as plt
#from ray import java_actor_class
#from scipy.fft import dst
from planning.generateMap import generateTasks
from .line import *
from math import pi
import numpy as np
from .Vec2d import Vec2d
from .utils import *
from math import cos, sin, tan
# from copy import deepcopy
from scipy.spatial import cKDTree
from planning.utilsPlanning import *
import time
import cv2 as cv

from planning.reedShepp import *


class State:
    def __init__(self, x, y, theta, v, steer):
        self.x = x
        self.y = y
        self.theta = theta
        self.v = v
        self.steer = steer
        self.width = 0
        self.length = 0

class VehicleConfig:
    def __init__(self, car_config):
        self.length = car_config["length"]
        self.width = car_config["width"]
        self.wheel_base = car_config["wheel_base"]
        self.safe_eps = car_config["safe_eps"]
        self.max_steer = degToRad(car_config["max_steer"])
        self.max_vel = car_config["max_vel"]
        self.min_vel = car_config["min_vel"]
        self.max_acc = car_config["max_acc"]
        self.max_ang_vel = car_config["max_ang_vel"]
        self.max_ang_acc = car_config["max_ang_acc"]
        self.delta_t = car_config["delta_t"]
        self.use_clip = car_config["use_clip"]
        self.jerk = car_config["jerk"]
        if self.jerk != 0:
            self.is_jerk = True
        else:
            self.is_jerk = False
        self.rear_to_center = (self.length - self.wheel_base) / 2.
        self.min_dist_to_check_collision = math.hypot(self.wheel_base / 2 \
                + self.length / 2., self.width / 2.)
        self.car_radius = math.hypot(self.length / 2., self.width / 2.)
        
    def dynamic(self, state, action):
        a = action[0]
        Eps = action[1]
        dt = self.delta_t
  
        if self.use_clip:
            a = np.clip(a, -self.max_acc, self.max_acc)
            Eps = np.clip(Eps, -self.max_ang_acc, self.max_ang_acc)
        self.a = a
        self.Eps = Eps
        
        dV = a * dt
        V = state.v + dV
        overSpeeding = V > self.max_vel or V < self.min_vel
        V = np.clip(V, self.min_vel, self.max_vel)

        dv_s = Eps * dt
        self.v_s = self.v_s + dv_s
        self.v_s = np.clip(self.v_s, -self.max_ang_vel, self.max_ang_vel)
        dsteer = self.v_s * dt
        steer = normalizeAngle(state.steer + dsteer)
        overSteering = abs(steer) > self.max_steer
        steer = np.clip(steer, -self.max_steer, self.max_steer)

        w = (V * np.tan(steer) / self.wheel_base)
        self.w = w
        dtheta = w * dt
        theta = normalizeAngle(state.theta + dtheta)

        dx = V * np.cos(theta) * dt
        dy = V * np.sin(theta) * dt
        x = state.x + dx
        y = state.y + dy

        new_state = State(x, y, theta, V, steer)

        self.prev_gear = self.gear
        if self.gear is None:
            if V > 0:
                self.gear = True
            elif V < 0:
                self.gear = False
        else:
            if self.gear:
                if V < 0:
                    self.gear = False
            else:
                if V > 0:
                    self.gear = True

            
        return new_state, overSpeeding, overSteering
    
    
    def shift_state(self, state, toCenter=False):
        l = self.length / 2
        shift = l - self.rear_to_center
        shift = -shift if toCenter else shift
        new_state = State(state.x + shift * cos(state.theta), 
                state.y + shift * sin(state.theta), state.theta, state.v, state.steer)
        return new_state

class ObsEnvironment(gym.Env):
    def __init__(self, full_env_name, config):
        self.gear_switch_penalty = True
        self.RS_reward = True
        self.adding_ego_features = True
        self.adding_dynamic_features = True
        self.gridCount = 4
        self.grid_resolution = 4
        self.grid_shape = (120, 120)
        assert self.grid_shape[0] % self.grid_resolution == 0 \
                   and self.grid_shape[1] % self.grid_resolution == 0, "incorrect grid shape"

        self.name = full_env_name
        env_config = config["our_env_config"]
        self.validate_env = env_config["validate_env"]
        self.validateTestDataset = env_config["validateTestDataset"]
        if self.validate_env:
            print("DEBUG: VALIDATE ENV")
            if self.validateTestDataset:
                print("DEBUG: VALIDATE TEST DATASET")
        self.reward_config = config["reward_config"]
        self.goal = None
        self.current_state = None
        self.old_state = None
        self.last_action = [0., 0.]
        self.obstacle_segments = []
        self.dyn_obstacle_segments = []
        self.last_observations = []
        self.stepCounter = 0
        self.vehicle = VehicleConfig(config['car_config'])
        self.trainTasks = config['tasks']
        self.valTasks = config['valTasks']
        self.maps_init = config['maps']
        self.maps = dict(config['maps'])
        self.alpha = env_config['alpha']
        self.max_steer = env_config['max_steer']
        self.max_dist = env_config['max_dist']
        self.min_dist = env_config['min_dist']
        self.min_vel = env_config['min_vel']
        self.max_vel = env_config['max_vel']
        self.min_obs_v = env_config['min_obs_v']
        self.max_obs_v = env_config['max_obs_v']
        self.HARD_EPS = env_config['HARD_EPS']
        self.MEDIUM_EPS = env_config['MEDIUM_EPS']
        self.SOFT_EPS = env_config['SOFT_EPS']
        self.ANGLE_EPS = degToRad(env_config['ANGLE_EPS'])
        self.SPEED_EPS = env_config['SPEED_EPS']
        self.STEERING_EPS = degToRad(env_config['STEERING_EPS'])
        self.dl_first_goal = env_config['dl_first_goal']
        self.MAX_DIST_LIDAR = env_config['MAX_DIST_LIDAR']
        self.UPDATE_SPARSE = env_config['UPDATE_SPARSE']
        self.view_angle = degToRad(env_config['view_angle'])
        self.hard_constraints = env_config['hard_constraints']
        self.medium_constraints = env_config['medium_constraints']
        self.soft_constraints = env_config['soft_constraints']
        self.affine_transform = env_config['affine_transform']
        self.with_potential = env_config['reward_with_potential']
        self.frame_stack = env_config['frame_stack']
        self.bias_beam = env_config['bias_beam']
        self.use_acceleration_penalties = env_config['use_acceleration_penalties']
        self.use_velocity_goal_penalty = env_config['use_velocity_goal_penalty']
        self.use_different_acc_penalty = env_config['use_different_acc_penalty']
        self._max_episode_steps = env_config['max_polamp_steps']
        self.dynamic_obstacles = []
        self.dynamic_obstacles_v_s = []
        self.dyn_acc = 0
        self.dyn_ang_vel = 0
        self.collision_time = 0
        self.reward_weights = [
            self.reward_config["collision"],
            self.reward_config["goal"],
            self.reward_config["timeStep"],
            self.reward_config["distance"],
            self.reward_config["overSpeeding"],
            self.reward_config["overSteering"],
            self.reward_config["gearSwitchPenalty"]
        ]
        if self.use_acceleration_penalties:
            self.reward_weights.append(self.reward_config["Eps_penalty"])
            self.reward_weights.append(self.reward_config["a_penalty"])
        if self.use_velocity_goal_penalty:
            self.reward_weights.append(self.reward_config["v_goal_penalty"])
        if self.use_different_acc_penalty:
            self.reward_weights.append(self.reward_config["differ_a"])
            self.reward_weights.append(self.reward_config["differ_Eps"])
        self.unionTask = env_config['union']
        self.union_without_forward_task = env_config['union_without_forward_task']
        assert not(not self.unionTask and self.union_without_forward_task), \
                "incorrect set union task: without forward but not union"
        assert self.unionTask and self.union_without_forward_task or \
               not self.unionTask and not self.union_without_forward_task, \
               f"forgot without forward task"
        if self.unionTask:
                self.second_goal = State(config["second_goal"][0], 
                                config["second_goal"][1],
                                config["second_goal"][2],
                                config["second_goal"][3],
                                config["second_goal"][4])
        assert self.hard_constraints \
            + self.medium_constraints \
            + self.soft_constraints == 1, "custom assert: only one constraint is acceptable"

        state_min_box = [[[-np.inf for j in range(self.grid_shape[1])] 
                for i in range(self.grid_shape[0])] for _ in range(self.gridCount)]
        state_max_box = [[[np.inf for j in range(self.grid_shape[1])] 
                for i in range(self.grid_shape[0])] for _ in range(self.gridCount)]
        obs_min_box = np.array(state_min_box)
        obs_max_box = np.array(state_max_box)
        self.observation_space = gym.spaces.Box(obs_min_box, obs_max_box, 
                                            dtype=np.float32)
        self.action_space = gym.spaces.Box(low=np.array([-1, -1]), 
                            high=np.array([1, 1]), dtype=np.float32)
        self.lst_keys = list(self.maps.keys())
        index = np.random.randint(len(self.lst_keys))
        self.map_key = self.lst_keys[index]
        self.obstacle_map = self.maps[self.map_key]
            
    def getBB(self, state, width=2.0, length=3.8, ego=True):
        x = state.x
        y = state.y
        angle = state.theta
        if ego:
            w = self.vehicle.width / 2
            l = self.vehicle.length / 2
        else:
            w = width
            l = length
        BBPoints = [(-l, -w), (l, -w), (l, w), (-l, w)]
        vertices = []
        sinAngle = math.sin(angle)
        cosAngle = math.cos(angle)
        for i in range(len(BBPoints)):
            new_x = cosAngle * (BBPoints[i][0]) - sinAngle * (BBPoints[i][1])
            new_y = sinAngle * (BBPoints[i][0]) + cosAngle * (BBPoints[i][1])
            vertices.append(Point(new_x + x, new_y + y))
            
        segments = [(vertices[(i) % len(vertices)], \
                    vertices[(i + 1) % len(vertices)]) 
                    for i in range(len(vertices))]
        
        return segments


    def __sendBeam(self, state, angle, nearestObstacles=None, 
                                with_angles=False, lst_indexes=[]):
        if nearestObstacles is None:
            nearestObstacles = list(self.obstacle_segments)
            nearestObstacles.extend(self.dyn_obstacle_segments)
        
        angle = normalizeAngle(angle + state.theta)
        new_x = state.x + self.MAX_DIST_LIDAR * cos(angle)
        new_y = state.y + self.MAX_DIST_LIDAR * sin(angle)
        p1 = Point(state.x, state.y)
        q1 = Point(new_x, new_y)
        min_dist = self.MAX_DIST_LIDAR
        for i, obstacles in enumerate(nearestObstacles):
            for obst_with_angles in obstacles:
                if with_angles:
                    angle1, angle2 = obst_with_angles[0]
                    p2, q2 = obst_with_angles[1]
                    if not angleIntersection(angle1, angle2, angle):
                        continue
                else:
                    p2, q2 = obst_with_angles

                if(doIntersect(p1, q1, p2, q2)):
                    beam = Line(p1, q1)
                    segment = Line(p2, q2)
                    intersection = beam.isIntersect(segment)
                    distance = math.hypot(p1.x - intersection.x, p1.y - intersection.y)
                    min_dist = min(min_dist, distance)
                    if (distance < self.vehicle.min_dist_to_check_collision):
                        if i not in lst_indexes and i < len(self.obstacle_segments):
                            lst_indexes.append(i)
                    
        return min_dist
    
    def getRelevantSegments(self, state, with_angles=False):
        relevant_obstacles = []
        obstacles = list(self.obstacle_segments)
        obstacles.extend(self.dyn_obstacle_segments)
        for obst in obstacles:
            new_segments = []
            for segment in obst:
                d1 = math.hypot(state.x - segment[0].x, state.y - segment[0].y)
                d2 = math.hypot(state.x - segment[1].x, state.y - segment[1].y)
                new_segments.append((min(d1, d2), segment)) 
            new_segments.sort(key=lambda s: s[0])
            new_segments = [pair[1] for pair in new_segments[:2]]
            if not with_angles:
                relevant_obstacles.append(new_segments)
            else:
                new_segments_with_angle = []
                angles = []
                for segment in new_segments:
                    angle1 = math.atan2(segment[0].y - state.y, segment[0].x - state.x)
                    angle2 = math.atan2(segment[1].y - state.y, segment[1].x - state.x)
                    min_angle = min(angle1, angle2)
                    max_angle = max(angle1, angle2)
                    new_segments_with_angle.append(((min_angle, max_angle), segment))
                    angles.append((min_angle, max_angle))
                if angleIntersection(angles[0][0], angles[0][1], angles[1][0]) and \
                    angleIntersection(angles[0][0], angles[0][1], angles[1][1]):
                    relevant_obstacles.append([new_segments_with_angle[0]])
                elif angleIntersection(angles[1][0], angles[1][1], angles[0][0]) and \
                    angleIntersection(angles[1][0], angles[1][1], angles[0][1]):
                    relevant_obstacles.append([new_segments_with_angle[1]])
                else:
                    relevant_obstacles.append(new_segments_with_angle)
                    
        return relevant_obstacles


    def getDiff(self, state):
        if self.goal is None:
            self.goal = state
        delta = []
        dx = self.goal.x - state.x
        dy = self.goal.y - state.y
        dtheta = self.goal.theta - state.theta
        dv = self.goal.v - state.v
        dsteer = self.goal.steer - state.steer
        theta = state.theta
        v = state.v
        steer = state.steer
        v_s = self.vehicle.v_s
        w = self.vehicle.w
        a = self.vehicle.a
        Eps = self.vehicle.Eps
        delta.extend([dx, dy, dtheta, dv, dsteer, theta, v, steer, v_s])
        
        return delta

    def transformTask(self, from_state, goal_state, 
                        obstacles, dynamic_obstacles=[]):
        
        if self.affine_transform:
            sx, sy, stheta, sv, sst = from_state
            gx, gy, gtheta, gv, gst = goal_state
            self.transform = Transformation()
            start_transform, goal_transform = self.transform.rotate([sx, sy, stheta], [gx, gy, gtheta])
            start_transform.append(sv)
            goal_transform.append(gv)
            start_transform.append(sst)
            goal_transform.append(gst)

            new_obstacle_map = []
            for index in range(len(obstacles)):
                x, y, theta, width, length = obstacles[index]
                state = self.transform.rotateState([x, y, theta])
                new_obstacle_map.append([state[0], state[1],  state[2], width, length])
            self.obstacle_map = new_obstacle_map

            new_dyn_obstacles = []
            for index in range(len(dynamic_obstacles)):
                x, y, theta, v, st = dynamic_obstacles[index]
                state = self.transform.rotateState([x, y, theta])
                new_dyn_obstacles.append(State(state[0], state[1], state[2], v, st))
                
            self.dynamic_obstacles = new_dyn_obstacles
        else:
            start_transform = list(from_state)
            goal_transform = list(goal_state)
            new_obstacle_map = []
            for index in range(len(obstacles)):
                state = obstacles[index]
                new_obstacle_map.append([state[0], state[1],  
                            state[2], state[3], state[4]])
            self.obstacle_map = new_obstacle_map

            new_dyn_obstacles = []
            for index in range(len(dynamic_obstacles)):
                state = dynamic_obstacles[index]
                new_dyn_obstacles.append(State(state[0], state[1], 
                            state[2], state[3], state[4]))
                
            self.dynamic_obstacles = new_dyn_obstacles

        start = State(start_transform[0], start_transform[1], 
            start_transform[2], start_transform[3], start_transform[4])
        goal = State(goal_transform[0], goal_transform[1], 
            goal_transform[2], goal_transform[3], goal_transform[4])
        
        return start, goal 

    def generateSimpleTask(self, obstacles=[]):
        if (len(obstacles) > 0):
            train_tasks = generateTasks(obstacles)
            start, goal = train_tasks[0]
        else:
            start_x = 0
            start_y = 0
            goal_x = np.random.randint(self.min_dist, self.max_dist + 1)
            goal_y = 0
            start_theta = degToRad(np.random.randint(-self.alpha, self.alpha + 1))
            goal_theta = degToRad(np.random.randint(-self.alpha, self.alpha + 1))
            start_v = 0
            goal_v = np.random.randint(self.min_vel, self.max_vel + 1)
            start_steer = degToRad(np.random.randint(-self.max_steer, self.max_steer + 1))
            goal_steer = 0
            start = [start_x, start_y, start_theta, start_v, start_steer]
            goal = [goal_x, goal_y, goal_theta, goal_v, goal_steer]
        
        return (start, goal)

    def setTask(self, tasks, idx, obstacles, rrt):
        if len(tasks) > 0:
            i = np.random.randint(len(tasks)) if idx is None else idx
            current_task = tuple(tasks[i])
            if(len(current_task) == 2):
                current, goal = current_task
            else:
                current, goal, dynamic_obstacles = current_task
                if not rrt:
                    if (np.random.randint(3) > 0):
                        for dyn_obst in dynamic_obstacles:
                            self.dynamic_obstacles.append(dyn_obst)
                            self.dynamic_obstacles_v_s.append(0)
                else:
                    for dyn_obst in dynamic_obstacles:
                        self.dynamic_obstacles.append(dyn_obst)
                        self.dynamic_obstacles_v_s.append(0)
        else:
            current, goal = self.generateSimpleTask(obstacles)

        self.current_state, self.goal = self.transformTask(current, goal, 
                                            obstacles, self.dynamic_obstacles)
        self.old_state = self.current_state

    def reset(self, idx=None, fromTrain=True, val_key=None, rrt=False):
        self.maps = dict(self.maps_init)
        if not self.validate_env:
            self.obst_random_actions = np.random.choice([True, 
                                                False, False, False, False])
        else:
            self.obst_random_actions = False
        self.stepCounter = 0
        self.last_observations = []
        self.last_action = [0., 0.]
        self.obstacle_segments = []
        self.dyn_obstacle_segments = []
        self.dynamic_obstacles = []
        self.dynamic_obstacles_v_s = []
        self.dyn_acc = 0
        self.dyn_ang_vel = 0
        self.dyn_ang_acc = 0
        self.vehicle.v_s = 0
        self.vehicle.w = 0
        self.vehicle.Eps = 0
        self.vehicle.a = 0
        self.vehicle.j_a = 0
        self.vehicle.j_Eps = 0
        self.vehicle.prev_a = 0
        self.vehicle.prev_Eps = 0
        self.collision_time = 0
        if self.RS_reward:
            self.new_RS = None

        self.vehicle.gear = None
        self.vehicle.prev_gear = None
        if self.validate_env:
            rrt = True
            if self.validateTestDataset:
                self.stop_dynamic_step = 500
                if val_key == "map0" or val_key == "map2":
                    self.stop_dynamic_step = 100
                elif val_key == "map1" or val_key == "map3":
                    self.stop_dynamic_step = 110
                elif val_key == "map6":
                    self.stop_dynamic_step = 110

        #DEBUG
        print("DEBUG:", val_key, self.stop_dynamic_step)

        if fromTrain:
            index = np.random.randint(len(self.lst_keys))
            self.map_key = self.lst_keys[index]
            self.obstacle_map = self.maps[self.map_key]
            tasks = self.trainTasks[self.map_key]
            self.setTask(tasks, idx, self.obstacle_map, rrt)
        else:
            self.map_key = val_key
            self.obstacle_map = self.maps[self.map_key]
            tasks = self.valTasks[self.map_key]
            self.setTask(tasks, idx, self.obstacle_map, rrt)

        #DEBUG
        print("DEBUG dynamic:", self.dynamic_obstacles)

        for obstacle in self.obstacle_map:
            obs = State(obstacle[0], obstacle[1], obstacle[2], 0, 0)
            width = obstacle[3]
            length = obstacle[4]
            self.obstacle_segments.append(self.getBB(obs, width=width, length=length, ego=False))
        if self.unionTask:    
            if self.union_without_forward_task:
                self.first_goal_reached = True
                self.goal = self.second_goal
            else:
                self.first_goal_reached = False
        else:
            self.first_goal_reached = True
        if self.goal.theta != degToRad(90):
            self.task = 1 #forward task
        else:
            self.task = -1 #backward task

        self.start_dist = self.__goalDist(self.current_state)

        self.last_images = []
        self.grid_static_obst = None
        self.grid_agent = None
        self.grid_with_adding_features = None

        observation = self.get_observation(first_obs=True)
    
        return observation
    
    
    def __reward(self, current_state, new_state, goalReached, 
                collision, overSpeeding, overSteering):
        if not self.RS_reward:
            previous_delta = self.__goalDist(current_state)
            new_delta = self.__goalDist(new_state)
        reward = []

        reward.append(-1 if collision else 0)

        if goalReached:
            reward.append(1)
        else:
            reward.append(0)
        if not (self.stepCounter % self.UPDATE_SPARSE):
            reward.append(-1)

            if self.RS_reward:
                if self.new_RS is None:
                    self.prev_RS = reedsSheppSteer(current_state, self.goal)
                else:
                    self.prev_RS = (self.new_RS[0].copy(), 
                                    self.new_RS[1].copy(), 
                                    self.new_RS[2].copy())
                self.new_RS = reedsSheppSteer(new_state, self.goal)

                if self.new_RS[2] is None or self.prev_RS[2] is None:
                    self.new_RS = None
                    self.prev_RS = None
                    reward.append(0)
                else:
                    RS_L_prev = abs(self.prev_RS[2][0]) + \
                            abs(self.prev_RS[2][1]) + abs(self.prev_RS[2][2])
                    RS_L_new = abs(self.new_RS[2][0]) + \
                            abs(self.new_RS[2][1]) + abs(self.new_RS[2][2])
                    self.RS_diff = RS_L_prev - RS_L_new
                    reward.append(RS_L_prev - RS_L_new)
            else:
                if (new_delta < 0.5):
                    new_delta = 0.5
                if self.with_potential:
                    #reward.append((previous_delta - new_delta) / new_delta)
                    reward.append(previous_delta - new_delta)
                else:
                    reward.append(previous_delta - new_delta)
            reward.append(-1 if overSpeeding else 0)
            reward.append(-1 if overSteering else 0)
            if self.use_acceleration_penalties:
                reward.append(-abs(self.vehicle.Eps))
                reward.append(-abs(self.vehicle.a))
            if self.use_velocity_goal_penalty:
                if goalReached:
                    reward.append(-abs(new_state.v))
                else:
                    reward.append(0)
            if self.use_different_acc_penalty:
                reward.append(-abs(self.vehicle.a - self.vehicle.prev_a))
                reward.append(-abs(self.vehicle.Eps - self.vehicle.prev_Eps))
        else:
            reward.append(0)
            reward.append(0)
            reward.append(0)
            reward.append(0)
            if self.use_acceleration_penalties:
                reward.append(0)
                reward.append(0)
            if self.use_velocity_goal_penalty:
                if goalReached:
                    reward.append(0)
                else:
                    reward.append(0)
            if self.use_different_acc_penalty:
                reward.append(0)
                reward.append(0)
        '''
        if self.use_acceleration_penalties:
            reward.append(-abs(self.vehicle.Eps))
            reward.append(-abs(self.vehicle.a))
        if self.use_velocity_goal_penalty:
            if goalReached:
                reward.append(-abs(new_state.v))
            else:
                reward.append(0)
        if self.use_different_acc_penalty:
            reward.append(-abs(self.vehicle.a - self.vehicle.prev_a))
            reward.append(-abs(self.vehicle.Eps - self.vehicle.prev_Eps))
        '''
        if self.gear_switch_penalty:
            if not(self.vehicle.prev_gear is None) and self.vehicle.prev_gear != self.vehicle.gear:
                reward.append(-1)
            else:
                reward.append(0)
        else:
            reward.append(0)

        return np.matmul(self.reward_weights, reward)

    def isCollision(self, state, min_beam, lst_indexes=[]):
        
        if (self.vehicle.min_dist_to_check_collision < min_beam):
            return False

        if len(self.obstacle_segments) > 0 or len(self.dyn_obstacle_segments) > 0:
            bounding_box = self.getBB(state)
            for i, obstacle in enumerate(self.obstacle_segments):
                # if i in lst_indexes:
                # print("Check collision")
                if (intersectPolygons(obstacle, bounding_box)):
                    return True
                    
            for obstacle in self.dyn_obstacle_segments:
                mid_x = (obstacle[0][0].x + obstacle[1][1].x) / 2.
                mid_y = (obstacle[0][0].y + obstacle[1][1].y) / 2.
                distance = math.hypot(mid_x - state.x, mid_y - state.y)    
                #if (distance > (self.vehicle.min_dist_to_check_collision)):
                dyn_obst_corner_x = obstacle[0][0].x
                dyn_obst_corner_y = obstacle[0][0].y
                dyn_obst_radius = math.hypot(mid_x - dyn_obst_corner_x, 
                                             mid_y - dyn_obst_corner_y)
                #if (distance > (self.vehicle.car_radius + dyn_obst_radius)):
                #    continue
                if (distance > (self.vehicle.min_dist_to_check_collision + dyn_obst_radius)):
                    continue
                if (intersectPolygons(obstacle, bounding_box)):
                    return True
            
        return False

    def __goalDist(self, state):
        return math.hypot(self.goal.x - state.x, self.goal.y - state.y)

    def obst_dynamic(self, state, action, previous_v_s, constant_forward=True):
        a = action[0]
        Eps = action[1]
        dt = self.vehicle.delta_t
        if constant_forward:
            a = 0
            Eps = 0
        if self.validate_env and self.validateTestDataset and \
                self.stepCounter >= self.stop_dynamic_step:
            a = 0
            Eps = 0
        elif self.validate_env and self.validateTestDataset and \
                (self.stepCounter + 5) >= self.stop_dynamic_step:
            a = -1
            Eps = 0

        dV = a * dt
        V = state.v + dV
        overSpeeding = V > self.vehicle.max_vel or V < self.vehicle.min_vel
        
        if not constant_forward:
            V = np.clip(V, self.vehicle.min_vel, self.vehicle.max_vel)

        dv_s = Eps * dt
        v_s = previous_v_s + dv_s
        v_s = np.clip(v_s, -self.vehicle.max_ang_vel, self.vehicle.max_ang_vel)
        dsteer = v_s * dt
        steer = normalizeAngle(state.steer + dsteer)
        overSteering = abs(steer) > self.vehicle.max_steer
        steer = np.clip(steer, -self.vehicle.max_steer, self.vehicle.max_steer)

        w = (V * np.tan(steer) / self.vehicle.wheel_base)
        dtheta = w * dt
        theta = normalizeAngle(state.theta + dtheta)

        dx = V * np.cos(theta) * dt
        dy = V * np.sin(theta) * dt
        x = state.x + dx
        y = state.y + dy

        new_state = State(x, y, theta, V, steer)

        return new_state, overSpeeding, overSteering, v_s


    def step(self, action, next_dyn_states=[]):
        # print(f"action: {action}")
        info = {}
        isDone = False
        new_state, overSpeeding, overSteering = \
                self.vehicle.dynamic(self.current_state, action)
        
        if len(self.dynamic_obstacles) > 0:
            dynamic_obstacles = []
            dynamic_obstacles_v_s = []
            #if not (self.stepCounter % self.UPDATE_SPARSE):
            #self.dyn_acc = np.random.randint(-self.vehicle.max_acc, self.vehicle.max_acc + 1)
            #self.dyn_ang_acc = np.random.randint(-self.vehicle.max_ang_acc, self.vehicle.max_ang_acc)

            for index, (dyn_obst, v_s) in enumerate(zip(self.dynamic_obstacles, 
                                                    self.dynamic_obstacles_v_s)):
                if len(next_dyn_states) > 0:
                    x, y, theta, v, st = next_dyn_states[index]
                    state = self.transform.rotateState([x, y, theta])
                    new_dyn_obst = State(state[0], state[1], state[2], v, st)
                else:
                    dyn_acc = np.random.random() * 2 * \
                            self.vehicle.max_acc - self.vehicle.max_acc
                    dyn_ang_acc = np.random.random() * 2 * \
                            self.vehicle.max_ang_acc - self.vehicle.max_ang_acc
                    constant_forward = not self.obst_random_actions
                    new_dyn_obst, _, _, new_v_s = self.obst_dynamic(dyn_obst, 
                                                    [dyn_acc, dyn_ang_acc], v_s,
                                                constant_forward=constant_forward)
                
                dynamic_obstacles.append(new_dyn_obst)
                dynamic_obstacles_v_s.append(new_v_s)
            self.dynamic_obstacles = dynamic_obstacles
            self.dynamic_obstacles_v_s = dynamic_obstacles_v_s
            
            
        self.current_state = new_state
        self.last_action = action

        
        observation = self.get_observation()

        #collision
        start_time = time.time()
        temp_grid_obst = self.grid_static_obst + self.grid_dynamic_obst
        collision = temp_grid_obst[self.grid_agent == 1].sum() > 0
        collision = collision or (self.grid_agent.sum() == 0)
        if collision:
            print("DEBUG COLLISION:", 
                temp_grid_obst[self.grid_agent == 1].sum())
        end_time = time.time()


        self.collision_time += (end_time - start_time)
        end_time = time.time()
        distanceToGoal = self.__goalDist(new_state)
        info["EuclideanDistance"] = distanceToGoal
        if self.unionTask and not self.first_goal_reached:
            if self.soft_constraints:
                goalReached = distanceToGoal < self.SOFT_EPS + self.dl_first_goal
            elif self.medium_constraints:
                goalReached = distanceToGoal < self.MEDIUM_EPS + self.dl_first_goal
            elif self.hard_constraints:
                goalReached = distanceToGoal < self.HARD_EPS + self.dl_first_goal\
                    and abs(new_state.v - self.goal.v) <= self.SPEED_EPS
        else:
            if self.hard_constraints:
                goalReached = distanceToGoal < self.HARD_EPS and abs(
                    normalizeAngle(new_state.theta - self.goal.theta)) < self.ANGLE_EPS \
                    and abs(new_state.v - self.goal.v) <= self.SPEED_EPS
            elif self.medium_constraints:
                goalReached = distanceToGoal < self.MEDIUM_EPS and abs(
                    normalizeAngle(new_state.theta - self.goal.theta)) < self.ANGLE_EPS
            elif self.soft_constraints:
                goalReached = distanceToGoal < self.SOFT_EPS

        if not self.validate_env:
            reward = self.__reward(self.old_state, new_state, 
                                goalReached, collision, overSpeeding, 
                                overSteering)
        else:
            reward = 0
        

        if not (self.stepCounter % self.UPDATE_SPARSE):
            self.old_state = self.current_state
        self.stepCounter += 1
        if goalReached or collision or (self._max_episode_steps == self.stepCounter):
            if self.unionTask:
                if goalReached:  
                    if not self.first_goal_reached:
                        self.first_goal_reached = True
                        self.goal = self.second_goal
                        self.start_dist = self.__goalDist(new_state)
                        self.task = -1
                        goalReached = False
                        isDone = False
                    else:
                        isDone = True
                        if collision:
                            info["Collision"] = True
                else:
                    isDone = True
                    if collision:
                        info["Collision"] = True
            else:
                isDone = True
                if collision:
                    info["Collision"] = True
        
        self.vehicle.prev_a = self.vehicle.a
        self.vehicle.prev_Eps = self.vehicle.Eps
        
        return observation, reward, isDone, info


    def drawBB(self, state, ego=True, draw_arrow=True, color="-c"):
        a = self.getBB(state, ego=ego)
        plt.plot([a[(i + 1) % len(a)][0].x for i in range(len(a) + 1)], [a[(i + 1) % len(a)][0].y for i in range(len(a) + 1)], color)
        if draw_arrow:
            plt.arrow(state[0], state[1], 2 * math.cos(state[2]), 2 * math.sin(state[2]), head_width=0.5, color='magenta')

    def drawObstacles(self, vertices, color="-b"):
        a = vertices
        plt.plot([a[(i + 1) % len(a)][0].x for i in range(len(a) + 1)], [a[(i + 1) % len(a)][0].y for i in range(len(a) + 1)], color)
        # if draw_arrow:
        #     plt.arrow(state[0], state[1], 2 * math.cos(state[2]), 2 * math.sin(state[2]), head_width=0.5, color='magenta')

    def render(self, reward, figsize=(8, 8), save_image=True):
        fig, ax = plt.subplots(figsize=figsize)

        x_delta = self.MAX_DIST_LIDAR
        y_delta = self.MAX_DIST_LIDAR

        x_min = self.current_state.x - x_delta
        x_max = self.current_state.x + x_delta
        ax.set_xlim(x_min, x_max)

        y_min = self.current_state.y - y_delta
        y_max = self.current_state.y + y_delta
        ax.set_ylim(y_min, y_max)
        
        if len(self.obstacle_segments) > 0:
            for obstacle in self.obstacle_segments:
                self.drawObstacles(obstacle)

        for dyn_obst in self.dynamic_obstacles:
            width = self.vehicle.width / 2 + 0.3
            length = self.vehicle.length / 2 + 0.1
            #width = self.vehicle.width / 2
            #length = self.vehicle.length / 2
            center_dyn_obst = self.vehicle.shift_state(dyn_obst)
            agentBB = self.getBB(center_dyn_obst, width=width, length=length, ego=False)
            #agentBB = self.getBB(center_dyn_obst)

            self.drawObstacles(agentBB)
            plt.arrow(dyn_obst.x, dyn_obst.y, 2 * math.cos(dyn_obst.theta), 2 * math.sin(dyn_obst.theta), head_width=0.5, color='magenta')
        
        ax.plot([self.current_state.x, self.goal.x], [self.current_state.y, self.goal.y], '--r')

        center_state = self.vehicle.shift_state(self.current_state)
        agentBB = self.getBB(center_state)
        self.drawObstacles(agentBB, color="-g")

        center_goal_state = self.vehicle.shift_state(self.goal)
        agentBB = self.getBB(center_goal_state)
        #self.drawObstacles(agentBB, color="-cyan")
        self.drawObstacles(agentBB, color="cyan")

        vehicle_heading = Vec2d(cos(center_state.theta),
                 sin(center_state.theta)) * self.vehicle.length / 2
        ax.arrow(self.current_state.x, self.current_state.y,
                 vehicle_heading.x, vehicle_heading.y, width=0.1, head_width=0.3,
                 color='red')

        goal_heading = Vec2d(cos(center_goal_state.theta),
             sin(center_goal_state.theta)) * self.vehicle.length / 2
        ax.arrow(self.goal.x, self.goal.y, goal_heading.x,
                 goal_heading.y, width=0.1, head_width=0.3, color='cyan')

        #for angle in self.angle_space:
        #    position = Vec2d(self.current_state.x, self.current_state.y)
        #    heading = Vec2d(cos(self.current_state.theta), sin(self.current_state.theta))
        #    heading = Ray(position, heading).rotate(angle).heading * self.__sendBeam(self.current_state, angle)

        #    ax.arrow(position.x, position.y, heading.x, heading.y, color='yellow')

        dx = self.goal.x - self.current_state.x
        dy = self.goal.y - self.current_state.y
        ds =  math.hypot(dx, dy)
        step_count = self.stepCounter
        theta = radToDeg(self.current_state.theta)
        v = self.current_state.v
        delta = radToDeg(self.current_state.steer)
        Eps = self.vehicle.Eps
        v_s = self.vehicle.v_s
        a = self.vehicle.a
        #j_a = self.vehicle.j_a
        #j_Eps = self.vehicle.j_Eps

        reeshep_dist = 0
        if self.RS_reward and not self.new_RS is None:
            if self.new_RS[2] is None:
               reeshep_dist = 0
            else:
                reeshep_dist = abs(self.new_RS[2][0]) + \
                    abs(self.new_RS[2][1]) + abs(self.new_RS[2][2])
                ax.plot([st[0] for st in self.new_RS[0]], 
                    [st[1] for st in self.new_RS[0]])
        else:
            reeshep_dist = 0
        #print("RS diff:", self.RS_diff)
        
        #ax.set_title(
        #    f'$dx={dx:.1f}, dy={dy:.1f}, E={Eps:.2f}, v_s={v_s:.2f}, \
        #    phi={theta:.0f}^\\circ, v={v:.2f} \, m/s, \
        #    steer={delta:.0f}^\\circ, a = {a:.2f}, m/s^2, r={reward:.0f}, \
        #    j_a = {j_a:.2f}, j_Eps = {j_Eps:.2f}$')
        #print("DEBUG", dx, dy, j_a, j_Eps, a, Eps)
        #ax.set_title(f'$dx={dx:.1f}, dy={dy:.1f}, j_a = {j_a:.2f}, j_Eps = {j_Eps:.2f}, a = {a:.2f}, E={Eps:.2f}, r={reward:.0f}$')
        #ax.set_title(f'$dx={dx:.1f}, dy={dy:.1f}, a = {a:.2f}, E={Eps:.2f}, v = {v:.2f}, v_s={v_s:.2f}, r={reward:.0f}, RS_d={reeshep_dist:.1f}$')
        ax.set_title(f'$step = {step_count:.0f}, ds = {ds:.1f}, gear = {self.vehicle.gear}, steer={delta:.0f}$ \n $\\theta = {theta:.0f}^\\circ, a = {a:.2f}, E={Eps:.2f}, v = {v:.2f}, v_s={v_s:.2f}, r={reward:.0f}, RS_d={reeshep_dist:.1f}$')
        
        
        
        if save_image:
            fig.canvas.draw()  # draw the canvas, cache the renderer
            image = np.frombuffer(fig.canvas.tostring_rgb(), dtype='uint8')
            image = image.reshape(fig.canvas.get_width_height()[::-1] + (3,))
            #image = image.reshape(1600, 2000, 3)
            plt.close('all')
            return image
        else:
            plt.pause(0.1)
            plt.show()
            # plt.close('all')

    def close(self):
        pass

    def get_observation(self, first_obs=False):
        fake_static_obstacles = False
        if len(self.obstacle_segments) == 0:
            fake_static_obstacles = True
            parking_height = 2.7
            parking_width = 4.5
            bottom_left_boundary_width = parking_width / 2
            bottom_right_boundary_width = parking_width / 2
            bottom_left_boundary_height = 6 # any value
            bottom_right_boundary_height = bottom_left_boundary_height
            bottom_left_boundary_center_x = 5 # any value
            bottom_right_boundary_center_x = bottom_left_boundary_center_x \
                        + bottom_left_boundary_height + parking_height \
                        + bottom_right_boundary_height
            bottom_left_boundary_center_y = -5.5 # init value
            bottom_right_boundary_center_y = bottom_left_boundary_center_y
            bottom_road_edge_y = bottom_left_boundary_center_y + bottom_left_boundary_width
            #upper_boundary_width = 2 # any value
            upper_boundary_width = 0.5 # any value
            upper_boundary_height = 17 
            bottom_down_width = upper_boundary_width 
            #bottom_down_height = upper_boundary_height 
            bottom_down_height = parking_height / 2 
            upper_boundary_center_x = bottom_left_boundary_center_x \
                            + bottom_left_boundary_height + parking_height / 2
            bottom_down_center_x = upper_boundary_center_x
            bottom_down_center_y = bottom_left_boundary_center_y \
                        - bottom_left_boundary_width - bottom_down_width \
                        - 0.2 # dheight
            road_width_ = 6
            bottom_left_right_dx_ = 0.25
            road_center_y = bottom_road_edge_y + road_width_
            upper_boundary_center_y = road_center_y + road_width_ + upper_boundary_width
            
            self.obstacle_map = [
                [upper_boundary_center_x, upper_boundary_center_y, 
                        0, upper_boundary_width, upper_boundary_height], 
                    [bottom_left_boundary_center_x + bottom_left_right_dx_, 
                    bottom_left_boundary_center_y, 0, bottom_left_boundary_width,
                    bottom_left_boundary_height],
                    [bottom_right_boundary_center_x - bottom_left_right_dx_, 
                    bottom_right_boundary_center_y, 0, bottom_right_boundary_width, 
                    bottom_right_boundary_height], 
                    [bottom_down_center_x, bottom_down_center_y, 
                    0, bottom_down_width, bottom_down_height]
                                ]

            for obstacle in self.obstacle_map:
                obs = State(obstacle[0], obstacle[1], obstacle[2], 0, 0)
                width = obstacle[3]
                length = obstacle[4]
                self.obstacle_segments.append(self.getBB(obs, width=width, length=length, ego=False))

        assert len(self.obstacle_segments) > 0, "not static env"


        grid_resolution = self.grid_resolution
        #if self.grid_obst is None:
        self.grid_static_obst = np.zeros(self.grid_shape)
        self.grid_dynamic_obst = np.zeros(self.grid_shape)
        self.grid_agent = np.zeros(self.grid_shape)
        self.grid_with_adding_features = np.zeros(self.grid_shape)
        
        #find static init point
        if first_obs:
            static_obsts_points = []
            for obstacle in self.obstacle_segments:
                static_obsts_points.extend(obstacle)

            x_min, y_min = static_obsts_points[0][0].x, static_obsts_points[0][0].y
            for point_ in static_obsts_points:         
                if point_[0].x < x_min:
                    x_min = point_[0].x
                if point_[0].y < y_min:
                    y_min = point_[0].y

            self.normalized_x_init = x_min
            self.normalized_y_init = y_min

        #get normalized static boxes
        if first_obs:
            self.normalized_static_boxes = []
            for obstacle in self.obstacle_segments:
                self.normalized_static_boxes.append([Point(pair_[0].x - self.normalized_x_init, 
                                            pair_[0].y - self.normalized_y_init) 
                                        for pair_ in obstacle])

        #get normalized dynamic boxes
        self.normalized_dynamic_boxes = []
        for dyn_obst in self.dynamic_obstacles:
            width = self.vehicle.width / 2 + 0.3
            length = self.vehicle.length / 2 + 0.1
            center_dyn_obst = self.vehicle.shift_state(dyn_obst)
            agentBB = self.getBB(center_dyn_obst, width=width, length=length, ego=False)
            self.normalized_dynamic_boxes.append([Point(max(0, pair_[0].x - self.normalized_x_init), 
                                         max(0, pair_[0].y - self.normalized_y_init)) 
                                    for pair_ in agentBB])
            

        center_state = self.vehicle.shift_state(self.current_state)
        agentBB = self.getBB(center_state)
        self.normalized_agent_box = [Point(max(0, pair_[0].x - self.normalized_x_init), 
                                         max(0, pair_[0].y - self.normalized_y_init)) 
                                    for pair_ in agentBB]

        center_goal_state = self.vehicle.shift_state(self.goal)
        agentBB = self.getBB(center_goal_state)
        self.normalized_goal_box = [Point(max(0, pair_[0].x - self.normalized_x_init), 
                                        max(0, pair_[0].y - self.normalized_y_init)) 
                                    for pair_ in agentBB]

        #choice grid indexes
        self.all_normilized_boxes = self.normalized_static_boxes.copy()
        self.all_normilized_boxes.extend(self.normalized_dynamic_boxes)
        self.all_normilized_boxes.append(self.normalized_agent_box)
        self.all_normilized_boxes.append(self.normalized_goal_box)

        x_shape, y_shape = self.grid_static_obst.shape
        self.cv_index_boxes = []
        for box_ in self.all_normilized_boxes:
            box_cv_indexes = []
            for i in range(len(box_)):
                prev_x, prev_y = box_[i - 1].x, box_[i - 1].y
                curr_x, curr_y = box_[i].x, box_[i].y
                next_x, next_y = box_[(i + 1) % len(box_)].x, box_[(i + 1) % len(box_)].y
                x_f, x_ceil = np.modf(curr_x)
                y_f, y_ceil = np.modf(curr_y)
                one_x_one = (int(x_ceil * grid_resolution), int(y_ceil * grid_resolution))
                one_x_one_x_ind = 0
                one_x_one_y_ind = 0
                
                rx, lx, ry, ly = 1.0, 0.0, 1.0, 0.0
                curr_ind_add = grid_resolution
                while rx - lx > 1 / grid_resolution:
                    curr_ind_add = curr_ind_add // 2
                    mx = (lx + rx) / 2
                    if x_f < mx:
                        rx = mx
                    else:
                        lx = mx
                        one_x_one_x_ind += curr_ind_add
                    my = (ly + ry) / 2
                    if y_f < my:
                        ry = my
                    else:
                        ly = my
                        one_x_one_y_ind += curr_ind_add

                if x_f == 0:
                    if prev_x <= curr_x and next_x <= curr_x:
                        x_ceil -= 1
                        one_x_one = (int(x_ceil * grid_resolution), int(y_ceil * grid_resolution))
                        one_x_one_x_ind = grid_resolution - 1

                if y_f == 0:
                    if prev_y <= curr_y and next_y <= curr_y:
                        y_ceil -= 1
                        one_x_one = (int(x_ceil * grid_resolution), int(y_ceil * grid_resolution))
                        one_x_one_y_ind = grid_resolution - 1

                index_grid_rev_x = one_x_one[0] + one_x_one_x_ind
                index_grid_rev_y = one_x_one[1] + one_x_one_y_ind
                
                cv_index_x = index_grid_rev_x
                cv_index_y = y_shape - index_grid_rev_y 
                box_cv_indexes.append(Point(cv_index_x, cv_index_y))
            self.cv_index_boxes.append(box_cv_indexes)

        self.cv_index_goal_box = self.cv_index_boxes.pop(-1)
        self.cv_index_agent_box = self.cv_index_boxes.pop(-1)
        
        #CV draw
        for ind_box, cv_box in enumerate(self.cv_index_boxes):
            contours = np.array([[cv_box[3].x, cv_box[3].y], [cv_box[2].x, cv_box[2].y], 
                                 [cv_box[1].x, cv_box[1].y], [cv_box[0].x, cv_box[0].y]])
            color = 1
            if ind_box >= len(self.normalized_static_boxes):
                #color = (ind_box - len(self.normalized_static_boxes) + 1) / 10
                self.grid_dynamic_obst = cv.fillPoly(self.grid_dynamic_obst, 
                                                pts = [contours], color=color)    
            self.grid_static_obst = cv.fillPoly(self.grid_static_obst, 
                                                pts = [contours], color=color)

        cv_box = self.cv_index_agent_box
        contours = np.array([[cv_box[3].x, cv_box[3].y], [cv_box[2].x, cv_box[2].y], 
                                [cv_box[1].x, cv_box[1].y], [cv_box[0].x, cv_box[0].y]])
        self.grid_agent = cv.fillPoly(self.grid_agent, pts = [contours], color=1)

        if not self.adding_ego_features:
            cv_box = self.cv_index_goal_box
            contours = np.array([[cv_box[3].x, cv_box[3].y], [cv_box[2].x, cv_box[2].y], 
                                    [cv_box[1].x, cv_box[1].y], [cv_box[0].x, cv_box[0].y]])
            self.grid_with_adding_features = cv.fillPoly(self.grid_with_adding_features, pts = [contours], color=1)
        else:
            adding_features = self.getDiff(self.current_state)
            self.grid_with_adding_features[0, 0:len(adding_features)] = adding_features
            if self.adding_dynamic_features:
                assert len(self.dynamic_obstacles) <= 2, "dynamic objects more than 2"
                for ind, dyn_state in enumerate(self.dynamic_obstacles):
                    self.grid_with_adding_features[ind + 1, 0:4] = [dyn_state.x - self.normalized_x_init, 
                                                                 dyn_state.y - self.normalized_y_init,
                                                                 dyn_state.theta,
                                                                 dyn_state.v]
            
        
        if fake_static_obstacles:
            self.grid_static_obst = np.zeros(self.grid_shape)
        dim_images = []
        dim_images.append(np.expand_dims(self.grid_static_obst, 0))
        dim_images.append(np.expand_dims(self.grid_dynamic_obst, 0))
        dim_images.append(np.expand_dims(self.grid_agent, 0))
        dim_images.append(np.expand_dims(self.grid_with_adding_features, 0))
        image = np.concatenate(dim_images, axis = 0)
        self.last_images.append(image)
        if first_obs:
            assert len(self.last_images) == 1, "incorrect init images"
            for _ in range(self.frame_stack - 1):
                self.last_images.append(image)
        else:
            self.last_images.pop(0)
        frames_images = np.concatenate(self.last_images, axis = 0)
        
        if fake_static_obstacles:
            self.obstacle_map = []
            self.obstacle_segments = []

        return frames_images


    def get_dim_image(self, dim, figsize=(0.5, 1)):
        fig, ax = plt.subplots(figsize=figsize)

        x_delta = self.MAX_DIST_LIDAR
        y_delta = self.MAX_DIST_LIDAR

        x_min = self.current_state.x - x_delta
        x_max = self.current_state.x + x_delta
        #ax.set_xlim(x_min, x_max)

        y_min = self.current_state.y - y_delta
        y_max = self.current_state.y + y_delta
        #ax.set_ylim(y_min, y_max)
        if dim == 1:
            if len(self.obstacle_segments) > 0:
                for obstacle in self.obstacle_segments:
                    self.drawObstacles(obstacle)
            
            for dyn_obst in self.dynamic_obstacles:
                width = self.vehicle.width / 2 + 0.3
                length = self.vehicle.length / 2 + 0.1
                center_dyn_obst = self.vehicle.shift_state(dyn_obst)
                agentBB = self.getBB(center_dyn_obst, width=width, length=length, ego=False)

                self.drawObstacles(agentBB)
                #plt.arrow(dyn_obst.x, dyn_obst.y, 2 * math.cos(dyn_obst.theta), 2 * math.sin(dyn_obst.theta), head_width=0.5, color='magenta')
        
        # ax.plot([self.current_state.x, self.goal.x], [self.current_state.y, self.goal.y], '--r')

        if dim == 0:
            center_state = self.vehicle.shift_state(self.current_state)
            agentBB = self.getBB(center_state)
            self.drawObstacles(agentBB, color="-g")
        if dim == 2:
            center_goal_state = self.vehicle.shift_state(self.goal)
            agentBB = self.getBB(center_goal_state)
            self.drawObstacles(agentBB, color="-r")

        fig.canvas.draw()  # draw the canvas, cache the renderer
        image = np.frombuffer(fig.canvas.tostring_rgb(), dtype='uint8')
        image = image.reshape(fig.canvas.get_width_height()[::-1] + (3,))
        #image = image.reshape(1600, 2000, 3)
        # plt.show()
        # plt.pause(10)
        plt.close('all')
        image = image[:, :, 0] + image[:, :, 1] + image[:, :, 2]
        image[image > 0] = 255

        return image
