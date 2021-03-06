import tensorflow as tf
import numpy as np
import os
import sys
import random
import subprocess
from redis import Redis
import time

sys.path.append(os.path.realpath(".."))

import helpers.utils as hlp
from models.feed_forward import FFDiscrete


class TRPODiscreteTrainer(FFDiscrete):
    def __init__(self, sess, args):
        FFDiscrete.__init__(self, sess, args)
        self.sess = sess
        self.config = args['config']
        self.env = args['environment']
        self.timesteps_per_launch = args['max_pathlength']
        self.n_workers = args['n_workers']
        self.distributed = args['distributed']
        self.timesteps_per_batch = args['timesteps_batch']
        self.n_tests = args['n_tests']
        self.max_kl = args['max_kl']
        self.normalize = args['normalize']
        self.scale = args['scale']
        self.gamma = args['gamma']
        self.value_updates = args['value_updates']
        self.save_every = args.get('save_every', 1)
        self.sums = self.sumsqrs = self.sumtime = 0
        self.timestep = 0
        self.create_internal()
        self.init_weights()
        self.train_scores = []
        self.test_scores = []
        np.set_printoptions(precision=6)

        # Worker parameters:
        self.id_worker = args['id_worker']
        self.test_mode = args['test_mode']

    def create_internal(self):
        self.targets = {
            "advantage": tf.placeholder(dtype=tf.float32, shape=[None]),
            "return": tf.placeholder(dtype=tf.float32, shape=[None]),
            "flat_tangent": tf.placeholder(dtype=tf.float32, shape=[None])
        }
        for i in range(len(self.n_actions)):
            self.targets["action_{}".format(i)] = tf.placeholder(dtype=tf.int32, shape=[None])
            self.targets["old_dist_{}".format(i)] = tf.placeholder(dtype=tf.float32, shape=[None, self.n_actions[i]])

        N = tf.shape(self.targets["advantage"])[0]
        base = [N] + [1 for _ in range(len(self.n_actions))]
        log_dist = tf.zeros(shape=[N] + self.n_actions)
        old_log_dist = tf.zeros(shape=[N] + self.n_actions)
        p_n = tf.zeros(shape=[N])
        old_p_n = tf.zeros(shape=[N])
        for i, n in enumerate(self.n_actions):
            right_shape = base[:]
            right_shape[i + 1] = n
            actions = self.targets["action_{}".format(i)]
            action_log_dist = tf.reshape(self.action_logprobs[i], [-1])
            p = tf.reshape(tf.gather(action_log_dist, tf.range(0, N) * n + actions), [-1])
            old_action_logdist = tf.reshape(self.targets["old_dist_{}".format(i)], [-1])
            old_p = tf.reshape(tf.gather(old_action_logdist, tf.range(0, N) * n + actions), [-1])
            p_n += p
            old_p_n += old_p
            log_dist += tf.reshape(action_log_dist, right_shape)
            old_log_dist += tf.reshape(old_action_logdist, right_shape)

        ratio = tf.exp(p_n - old_p_n)
        N = tf.cast(N, tf.float32)

        self.loss = -tf.reduce_mean(ratio * self.targets["advantage"])
        self.KL = tf.reduce_sum(tf.exp(old_log_dist) * (old_log_dist - log_dist)) / N
        self.entropy = tf.reduce_sum(-tf.exp(log_dist) * log_dist) / N

        self.policy_grad = hlp.flatgrad(self.loss, self.weights)
        KL_firstfixed = tf.reduce_sum(tf.stop_gradient(tf.exp(log_dist)) * (tf.stop_gradient(log_dist) - log_dist)) / N
        kl_ff_grads = tf.gradients(KL_firstfixed, self.weights)
        w_shapes = list(map(hlp.var_shape, self.weights))
        start = 0
        tangents = []
        for shape in w_shapes:
            size = np.prod(shape)
            param = tf.reshape(self.targets["flat_tangent"][start:(start + size)], shape)
            tangents.append(param)
            start += size
        gvp = [tf.reduce_sum(g * t) for (g, t) in zip(kl_ff_grads, tangents)]
        self.fisher_vector_product = hlp.flatgrad(gvp, self.weights)

        self.get_flat = hlp.GetFlat(self.weights, self.sess)
        self.set_from_flat = hlp.SetFromFlat(self.weights, self.sess)

        value_loss = tf.reduce_mean((self.targets["return"] - self.value) ** 2)

        self.value_train_op = tf.train.AdamOptimizer(0.05).minimize(value_loss, var_list=self.value_weights)

    def save(self, name):
        directory = 'saves/' + name + '/'
        if not os.path.exists(directory):
            os.makedirs(directory)
        directory += 'iteration_{}'.format(self.timestep) + '/'
        if not os.path.exists(directory):
            os.makedirs(directory)

        for i, tensor in enumerate(tf.global_variables()):
            value = self.sess.run(tensor)
            np.save(directory + 'weight_{}'.format(i), value)

        if self.scale != 'off':
            np.save(directory + 'sums', self.sums)
            np.save(directory + 'sumsquares', self.sumsqrs)
            np.save(directory + 'sumtime', self.sumtime)

        np.save(directory + 'timestep', np.array([self.timestep]))
        np.save(directory + 'train_scores', np.array(self.train_scores))
        np.save(directory + 'test_scores', np.array(self.test_scores))
        print("Agent successfully saved in folder {}".format(directory))

    def load(self, name, iteration=None):
        try:
            directory = 'saves/' + name + '/'
            if not os.path.exists(directory):
                print('That directory does not exist!')
                raise Exception
            if iteration is None:
                iteration = np.max([int(x[10:]) for x in [dir for dir in os.walk(directory)][0][1]])
            directory += 'iteration_{}'.format(iteration) + '/'

            for i, tensor in enumerate(tf.global_variables()):
                arr = np.load(directory + 'weight_{}.npy'.format(i))
                self.sess.run(tensor.assign(arr))

            if self.scale != 'off':
                self.sums = np.load(directory + 'sums.npy')
                self.sumsqrs = np.load(directory + 'sumsquares.npy')
                self.sumtime = np.load(directory + 'sumtime.npy')

            self.timestep = np.load(directory + 'timestep.npy')[0]
            self.train_scores = np.load(directory + 'train_scores.npy').tolist()
            self.test_scores = np.load(directory + 'test_scores.npy').tolist()

            print("Agent successfully loaded from folder {}".format(directory))
        except:
            print("Something is wrong, loading failed")

    def init_weights(self):
        self.sess.run(tf.global_variables_initializer())
        init_weights = [self.sess.run(w) for w in self.weights]
        self.set_weights(init_weights)

    def make_rollout(self):
        variables_server = Redis(port=12000)
        if self.scale != 'off':
            try:
                means = hlp.load_object(variables_server.get("means"))
                stds = hlp.load_object(variables_server.get("stds"))
                self.sess.run(self.norm_set_op, feed_dict=dict(zip(self.norm_phs, [means, stds])))
            except:
                pass
        try:
            weights = [hlp.load_object(variables_server.get("weight_{}".format(i))) for i in
                       range(len(self.weights))]
            self.set_weights(weights)
        except:
            pass
        env = self.env
        if self.test_mode:
            n_tasks = self.n_tests
            timesteps_per_worker = 100000000
        else:
            n_tasks = 10000
            timesteps_per_worker = self.timesteps_per_batch // self.n_workers

        timestep = 0
        i_task = 0

        paths = []
        while timestep < timesteps_per_worker and i_task < n_tasks:
            path = {}
            observations, action_tuples, rewards, dist_tuples, timestamps = [], [], [], [], []
            sums = np.zeros((1, env.get_observation_space()))
            sumsqrs = np.zeros(sums.shape)

            env.reset()
            while not env.done and env.timestamp < self.timesteps_per_launch:
                sums += env.features
                sumsqrs += np.square(env.features)
                observations.append(env.features[0])
                timestamps.append(env.timestamp)

                if not self.test_mode:
                    actions, dist_tuple = self.act(env.features, return_dists=True)
                    dist_tuples.append(dist_tuple)
                else:
                    actions = self.act(env.features, exploration=False)
                env.step(actions)
                timestep += 1

                action_tuples.append(actions)
                rewards.append(env.reward)

            path["observations"] = np.array(observations)
            path["action_tuples"] = np.array(action_tuples)
            path["rewards"] = np.array(rewards)
            if not self.test_mode:
                path["dist_tuples"] = np.array(dist_tuples)
            path["timestamps"] = np.array(timestamps)
            path["sumobs"] = sums
            path["sumsqrobs"] = sumsqrs
            path["terminated"] = env.done
            path["total"] = env.get_total_reward()
            paths.append(path)
            i_task += 1

        if self.distributed:
            variables_server.set("paths_{}".format(self.id_worker), hlp.dump_object(paths))
        else:
            self.paths = paths

    def train(self):
        cmd_server = 'redis-server --port 12000'
        p = subprocess.Popen(cmd_server, shell=True, preexec_fn=os.setsid)
        self.variables_server = Redis(port=12000)
        means = "-"
        stds = "-"
        if self.scale != 'off':
            if self.timestep == 0:
                print("Time to measure features!")
                if self.distributed:
                    worker_args = \
                        {
                            'config': self.config,
                            'test_mode': False,
                        }
                    hlp.launch_workers(worker_args, self.n_workers)
                    paths = []
                    for i in range(self.n_workers):
                        paths += hlp.load_object(self.variables_server.get("paths_{}".format(i)))
                else:
                    self.test_mode = False
                    self.make_rollout()
                    paths = self.paths

                for path in paths:
                    self.sums += path["sumobs"]
                    self.sumsqrs += path["sumsqrobs"]
                    self.sumtime += path["observations"].shape[0]

            stds = np.sqrt((self.sumsqrs - np.square(self.sums) / self.sumtime) / (self.sumtime - 1))
            means = self.sums / self.sumtime
            print("Init means: {}".format(means))
            print("Init stds: {}".format(stds))
            self.variables_server.set("means", hlp.dump_object(means))
            self.variables_server.set("stds", hlp.dump_object(stds))
            self.sess.run(self.norm_set_op, feed_dict=dict(zip(self.norm_phs, [means, stds])))
        while True:
            print("Iteration {}".format(self.timestep))
            start_time = time.time()

            if self.distributed:
                weights = self.get_weights()
                for i, weight in enumerate(weights):
                    self.variables_server.set("weight_" + str(i), hlp.dump_object(weight))
                worker_args = \
                    {
                        'config': self.config,
                        'test_mode': False,
                    }
                hlp.launch_workers(worker_args, self.n_workers)
                paths = []
                for i in range(self.n_workers):
                    paths += hlp.load_object(self.variables_server.get("paths_{}".format(i)))
            else:
                self.test_mode = False
                self.make_rollout()
                paths = self.paths

            observations = np.concatenate([path["observations"] for path in paths])
            actions = np.concatenate([path["action_tuples"] for path in paths])
            action_dists = []
            for _ in range(len(self.n_actions)):
                action_dists.append([])
            returns = []
            advantages = []
            for path in paths:
                self.sums += path["sumobs"]
                self.sumsqrs += path["sumsqrobs"]
                self.sumtime += path["rewards"].shape[0]
                dists = path["dist_tuples"]

                for i in range(len(self.n_actions)):
                    action_dists[i] += [dist[i][0] for dist in dists]
                returns += hlp.discount(path["rewards"], self.gamma, path["timestamps"]).tolist()
                values = self.sess.run(self.value, feed_dict={self.state_input: path["observations"]})
                values = np.append(values, 0 if path["terminated"] else values[-1])
                deltas = (path["rewards"] + self.gamma * values[1:] - values[:-1])
                advantages += hlp.discount(deltas, self.gamma, path["timestamps"]).tolist()
            returns = np.array(returns)
            advantages = np.array(advantages)

            if self.normalize == 'ranks':
                ranks = np.zeros_like(advantages)
                ranks[np.argsort(advantages)] = np.arange(ranks.shape[0], dtype=np.float32) / (ranks.shape[0] - 1)
                ranks -= 0.5
                advantages = ranks[:]
            elif self.normalize == 'center':
                advantages -= np.mean(advantages)
                advantages /= (np.std(advantages, ddof=1) + 0.001)

            feed_dict = {self.state_input: observations,
                         self.targets["return"]: returns,
                         self.targets["advantage"]: advantages}

            for i in range(len(self.n_actions)):
                feed_dict[self.targets["old_dist_{}".format(i)]] = np.array(action_dists[i])
                feed_dict[self.targets["action_{}".format(i)]] = actions[:, i]

            for i in range(self.value_updates):
                self.sess.run(self.value_train_op, feed_dict)

            train_rewards = np.array([path["rewards"].sum() for path in paths])
            train_lengths = np.array([len(path["rewards"]) for path in paths])

            thprev = self.get_flat()

            def fisher_vector_product(p):
                feed_dict[self.targets["flat_tangent"]] = p
                return self.sess.run(self.fisher_vector_product, feed_dict) + 0.1 * p

            g = self.sess.run(self.policy_grad, feed_dict)
            stepdir = hlp.conjugate_gradient(fisher_vector_product, -g)

            shs = .5 * stepdir.dot(fisher_vector_product(stepdir))
            lm = np.sqrt(shs / self.max_kl)
            fullstep = stepdir / (lm + 1e-18)

            def loss_kl(th):
                self.set_from_flat(th)
                return self.sess.run([self.loss, self.KL], feed_dict=feed_dict)

            theta = hlp.linesearch(loss_kl, thprev, fullstep, self.max_kl)
            self.set_from_flat(theta)

            lossafter, kloldnew = self.sess.run([self.loss, self.KL], feed_dict=feed_dict)

            print("Time for testing!")

            if self.distributed:
                weights = self.get_weights()
                for i, weight in enumerate(weights):
                    self.variables_server.set("weight_" + str(i), hlp.dump_object(weight))
                worker_args = \
                    {
                        'config': self.config,
                        'test_mode': True,
                    }
                hlp.launch_workers(worker_args, self.n_workers)
                paths = []
                for i in range(self.n_workers):
                    paths += hlp.load_object(self.variables_server.get("paths_{}".format(i)))
            else:
                self.test_mode = True
                self.make_rollout()
                paths = self.paths

            total_rewards = np.array([path["total"] for path in paths])
            eplens = np.array([len(path["rewards"]) for path in paths])

            if self.scale != 'full':
                stds = np.sqrt((self.sumsqrs - np.square(self.sums) / self.sumtime) / (self.sumtime - 1))
                means = self.sums / self.sumtime
                self.variables_server.set("means", hlp.dump_object(means))
                self.variables_server.set("stds", hlp.dump_object(stds))
                self.sess.run(self.norm_set_op, feed_dict=dict(zip(self.norm_phs, [means, stds])))

            print("""
-------------------------------------------------------------
Mean test score:           {test_scores}
Mean train score:          {train_scores}
Mean test episode length:  {test_eplengths}
Mean train episode length: {train_eplengths}
Max test score:            {max_test}
Max train score:           {max_train}
KL between old and new     {kl}
Loss after update          {loss}
Mean of features:          {means}
Std of features:           {stds}
-------------------------------------------------------------
                """.format(
                means=means,
                stds=stds,
                test_scores=np.mean(total_rewards),
                test_eplengths=np.mean(eplens),
                train_scores=np.mean(train_rewards),
                train_eplengths=np.mean(train_lengths),
                max_test=np.max(total_rewards),
                max_train=np.max(train_rewards),
                kl=kloldnew,
                loss=lossafter
            ))
            self.timestep += 1
            self.train_scores.append(np.mean(train_rewards))
            self.test_scores.append(np.mean(total_rewards))
            if self.timestep % self.save_every == 0:
                self.save(self.config[:-5])
