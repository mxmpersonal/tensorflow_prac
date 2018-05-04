#!/bin/python2.7

import os, sys
import time

import numpy as np
import tensorflow as tf

import data_iterator
from collections import namedtuple

flags = tf.flags
flags.DEFINE_string("train_path", None, "Where the training data is stored.")
flags.DEFINE_string("infer_path", None, "Where the infer data is stored.")
flags.DEFINE_string("model_path", None, "where the model data is stored.")
flags.DEFINE_string("out_embedding", None, "where the embedding data is stored.")
flags.DEFINE_string("out_softmax", None, "where the softmax weights data is stored.")
flags.DEFINE_string("config", "title", "which config is choosen.")
FLAGS = flags.FLAGS


class TitleConfig(object):
    vocab_size = 10000
    batch_size = 128
    num_steps = 30
    init_scale = 0.05
    lr = 1.0
    lr_decay = 0.5
    lr_nodecay_step = 2
    keep_prob = 1.0
    max_grad_norm = 5
    num_layers = 2
    hidden_size = 128
    embedding_size = 128
    max_epoch = 5


class ClickConfig(object):
    vocab_size = 40000
    batch_size = 256
    num_steps = 30
    init_scale = 0.05
    lr = 1.0
    lr_decay = 0.5
    lr_nodecay_step = 3
    keep_prob = 0.8
    max_grad_norm = 5
    num_layers = 2
    hidden_size = 128
    embedding_size = 128
    max_epoch = 5


class SeqModel(object):
    def __init__(self, is_training, config, iterator):
        self._is_training = is_training
        self._config = config
        self.batch_size = config.batch_size
        self.num_steps = config.num_steps

        vocab_size = config.vocab_size
        embedding_size = config.embedding_size
        hidden_size = config.hidden_size

        initializer = tf.random_uniform_initializer(-config.init_scale, config.init_scale)
        tf.get_variable_scope().set_initializer(initializer)

        with tf.device("/cpu:0"):
            self.embedding = tf.get_variable("embedding", [vocab_size, embedding_size], dtype=tf.float32)
            self.inputs = tf.nn.embedding_lookup(self.embedding, iterator.source)
        if is_training and config.keep_prob < 1:
            self.inputs = tf.nn.dropout(self.inputs, config.keep_prob)
        self.output, self.state = self._build_rnn_graph(self.inputs, iterator.source_len)
        reshape_output = tf.reshape(tf.concat(self.output, 1), [-1, config.hidden_size])

        ## last hidden output
        _, hidden_output = self.state[config.num_layers - 1]
        self._norm_hidden_output = tf.nn.l2_normalize(hidden_output, dim=1)
        ## mean pooling layer
        reshape_xlen = tf.cast(tf.reshape(iterator.source_len, [-1, 1]), dtype=tf.float32)
        mean_pooling = tf.divide(tf.reduce_sum(self.output, axis=1), reshape_xlen)
        self._norm_pooling = tf.nn.l2_normalize(mean_pooling, dim=1)

        self.softmax_w = tf.get_variable("softmax_w", [hidden_size, vocab_size], dtype=tf.float32)
        self.softmax_b = tf.get_variable("softmax_b", [vocab_size], dtype=tf.float32)
        self._norm_softmax_w = tf.nn.l2_normalize(self.softmax_w, dim=0)
        logits = tf.nn.xw_plus_b(reshape_output, self.softmax_w, self.softmax_b)
        logits = tf.reshape(logits, [self.batch_size, self.num_steps, vocab_size])
        masks = tf.sequence_mask(iterator.source_len, self.num_steps, dtype=tf.float32)
        loss = tf.contrib.seq2seq.sequence_loss(logits,
                                                iterator.target,
                                                masks,
                                                average_across_timesteps=False,
                                                average_across_batch=True)
        self._cost = tf.reduce_sum(loss)
        self._predict_count = tf.reduce_sum(iterator.source_len)
        self._final_state = self.state
        self.global_step = tf.Variable(0, trainable=False)
        self.saver = tf.train.Saver(tf.global_variables())

        if not is_training:
            return

        self._lr = tf.Variable(0.0, trainable=False)
        tvars = tf.trainable_variables()
        grads, _ = tf.clip_by_global_norm(tf.gradients(self._cost, tvars), config.max_grad_norm)
        optimizer = tf.train.GradientDescentOptimizer(self._lr)
        self._train_op = optimizer.apply_gradients(zip(grads, tvars), global_step=self.global_step)
        self._new_lr = tf.placeholder(tf.float32, shape=[], name="new_learning_rate")
        self._lr_update = tf.assign(self._lr, self._new_lr)

    def _build_rnn_graph(self, inputs, inputs_len):
        config = self._config
        is_training = self._is_training
        cell = tf.contrib.rnn.BasicLSTMCell(config.hidden_size, forget_bias=0.0, state_is_tuple=True)
        if is_training and config.keep_prob < 1:
            cell = tf.contrib.rnn.DropoutWrapper(cell, output_keep_prob=config.keep_prob)
        cells = tf.contrib.rnn.MultiRNNCell([cell] * config.num_layers, state_is_tuple=True)
        self._initial_state = cells.zero_state(config.batch_size, tf.float32)
        output, state = tf.nn.dynamic_rnn(cells, inputs, sequence_length=inputs_len, initial_state=self._initial_state)
        return output, state

    def assign_lr(self, sess, lr_value):
        sess.run(self._lr_update, feed_dict={self._new_lr: lr_value})

    @property
    def input(self):
        return self._input

    @property
    def cost(self):
        return self._cost

    @property
    def predict_count(self):
        return self._predict_count

    @property
    def lr(self):
        return self._lr

    @property
    def train_op(self):
        return self._train_op

    @property
    def initial_state(self):
        return self._initial_state

    @property
    def final_state(self):
        return self._final_state

    @property
    def norm_pooling(self):
        return self._norm_pooling

    @property
    def norm_hidden_output(self):
        return self._norm_hidden_output

    @property
    def norm_softmax_w(self):
        return self._norm_softmax_w


class Model(namedtuple("Model", ("graph", "model", "iterator"))):
    pass


def create_model(model_creator, config, file_path, is_training, is_infer, scope):
    graph = tf.Graph()
    with graph.as_default(), tf.container(scope):
        dataset = tf.data.TextLineDataset(file_path)
        if is_infer:
            iterator = data_iterator.get_iterator(dataset, config.batch_size, config.num_steps)
        else:
            iterator = data_iterator.get_iterator(dataset, config.batch_size, config.num_steps)
        model = model_creator(is_training=is_training, config=config, iterator=iterator)
    return Model(graph=graph, model=model, iterator=iterator)


def create_or_load_model(model, model_dir, sess, name):
    latest_ckpt = tf.train.latest_checkpoint(model_dir)
    start_time = time.time()
    sess.run(tf.global_variables_initializer())
    if latest_ckpt:
        model.saver.restore(sess, latest_ckpt)
        sess.run(tf.tables_initializer())
        print ("load %s model from %s, time %.2f" % (name, latest_ckpt, time.time() - start_time))
    else:
        sess.run(tf.tables_initializer())
        print ("create %s model, time %.2f" % (name, time.time() - start_time))
    global_step = model.global_step.eval(session=sess)
    return model, global_step


def run_epoch(sess, model, eval_op=None, verbose=False):
    costs = 0.0
    iters = 0
    batch_size = model.batch_size
    state = sess.run(model.initial_state)
    feed_dict = {}
    for i, (c, h) in enumerate(model.initial_state):
        feed_dict[c] = state[i].c
        feed_dict[h] = state[i].h
    fetches = {"cost": model.cost, "predict_count": model.predict_count}
    if eval_op is not None:
        fetches["eval_op"] = eval_op
    step = 0
    while True:
        try:
            vals = sess.run(fetches, feed_dict)
        except tf.errors.OutOfRangeError:
            print("Finished this epoch")
            break
        cost = vals['cost'] * batch_size
        predict_count = vals['predict_count']
        costs += cost
        iters += predict_count
        step += 1
        if step % 100 == 0:
            perplexity = np.exp(costs / iters)
            print("  step: %d, perplexity: %.3f" % (step, perplexity))
            sys.stdout.flush()

    return sess.run(model.global_step), np.exp(costs / iters)

def infer_pooling(sess, model, eval_op=None, verbose=False):
    costs = 0.0
    iters = 0
    batch_size = model.batch_size
    state = sess.run(model.initial_state)
    feed_dict = {}
    for i, (c, h) in enumerate(model.initial_state):
        feed_dict[c] = state[i].c
        feed_dict[h] = state[i].h
    fetches = {"cost": model.cost, "predict_count": model.predict_count}
    if eval_op is not None:
        fetches["eval_op"] = eval_op
    step = 0
    while True:
        try:
            vals = sess.run(fetches, feed_dict)
        except tf.errors.OutOfRangeError:
            print("Finished this epoch")
            break
        cost = vals['cost'] * batch_size
        predict_count = vals['predict_count']
        costs += cost
        iters += predict_count
        step += 1
        if step % 100 == 0:
            perplexity = np.exp(costs / iters)
            print("  step: %d, perplexity: %.3f" % (step, perplexity))
            sys.stdout.flush()

    return sess.run(model.global_step), np.exp(costs / iters)

# def infer_pooling(sess, model):
#
#    print "infer pooling"
#    poolings = []
#    for step in range(model.input.epoch_size):
#        state = sess.run(model.initial_state)
#        feed_dict = {}
#        for i, (c, h) in enumerate(model.initial_state):
#            feed_dict[c] = state[i].c
#            feed_dict[h] = state[i].h
#        polling = sess.run(model.norm_pooling, feed_dict)
#        for row in polling:
#            format_row = ",".join([str(_) for _ in row])
#            poolings.append(format_row)
#        if step % (model.input.epoch_size // 10) == 10:
#            progress = step * 1.0 / model.input.epoch_size
#            print("progress: %.3f" % progress)
#            sys.stdout.flush()
#    return poolings
#
# def infer_hidden_output(sess, model):
#
#    print "infer hidden output"
#    outputs = []
#    for step in range(model.input.epoch_size):
#        state = sess.run(model.initial_state)
#        feed_dict = {}
#        for i, (c, h) in enumerate(model.initial_state):
#            feed_dict[c] = state[i].c
#            feed_dict[h] = state[i].h
#        output = sess.run(model.norm_hidden_output, feed_dict)
#        for row in output:
#            format_row = ",".join([str(_) for _ in row])
#            outputs.append(format_row)
#        if step % (model.input.epoch_size // 10) == 10:
#            progress = step * 1.0 / model.input.epoch_size
#            print("progress: %.3f" % progress)
#            sys.stdout.flush()
#    return outputs

def main(_):
    print ("train_path: ", FLAGS.train_path)
    print ("infer_path: ", FLAGS.infer_path)
    print ("model_path: ", FLAGS.model_path)
    print ("out_embedding: ", FLAGS.out_embedding)
    print ("out_softmax: ", FLAGS.out_softmax)
    print ("config: ", FLAGS.config)

    train_config = TitleConfig() if FLAGS.config == "title" else ClickConfig()
    train_config = TitleConfig() if FLAGS.config == "title" else ClickConfig()
    infer_config = TitleConfig() if FLAGS.config == "title" else ClickConfig()
    test_config = TitleConfig() if FLAGS.config == "title" else ClickConfig()
    test_config.batch_size = 1
    test_config.num_steps = 1

    config_proto = tf.ConfigProto(log_device_placement=False, allow_soft_placement=True)
    if FLAGS.infer_path:
        infer_model = create_model(SeqModel, infer_config, FLAGS.infer_path, False, True, 'infer')
        infer_sess = tf.Session(target='', config=config_proto, graph=infer_model.graph)
    else:
        train_file_path = FLAGS.train_path + "/train"
        valid_file_path = FLAGS.train_path + "/valid"
        test_file_path = FLAGS.train_path + "/test"
        train_model = create_model(SeqModel, train_config, train_file_path, True, False, 'train')
        valid_model = create_model(SeqModel, train_config, valid_file_path, False, False, 'valid')
        test_model = create_model(SeqModel, test_config, valid_file_path, False, False, 'test')
        train_sess = tf.Session(target='', config=config_proto, graph=train_model.graph)
        valid_sess = tf.Session(target='', config=config_proto, graph=valid_model.graph)
        test_sess = tf.Session(target='', config=config_proto, graph=test_model.graph)

    if FLAGS.infer_path:
        #### infer progress
        pass
    else:
        #### training progress
        with train_model.graph.as_default():
            loaded_train_model, global_step = create_or_load_model(
                train_model.model, FLAGS.model_path, train_sess, "train")
        for i in range(train_config.max_epoch):
            lr_decay = train_config.lr_decay ** max(i + 1 - train_config.lr_nodecay_step, 0.0)
            loaded_train_model.assign_lr(train_sess, train_config.lr * lr_decay)
            print("epoch %d, learning rate is: %.3f" % (i + 1, train_sess.run(loaded_train_model.lr)))
            train_sess.run(train_model.iterator.initializer)
            global_step, train_perplexity = run_epoch(train_sess, loaded_train_model,
                                                      eval_op=loaded_train_model.train_op)
            loaded_train_model.saver.save(train_sess, os.path.join(FLAGS.model_path, "model.ckpt"),
                                          global_step=global_step)
            print("Epoch: %d, Train perplexity: %.3f" % (i + 1, train_perplexity))
            ### valid
            valid_sess.run(valid_model.iterator.initializer)
            with valid_model.graph.as_default():
                loaded_valid_model, global_step = create_or_load_model(
                    valid_model.model, FLAGS.model_path, valid_sess, "valid")
            _, valid_perplexity = run_epoch(valid_sess, loaded_valid_model)
            print("Epoch: %d Valid Perplexity: %.3f" % (i + 1, valid_perplexity))
        ### test
        test_sess.run(test_model.iterator.initializer)
        with test_model.graph.as_default():
            loaded_test_model, global_step = create_or_load_model(
                test_model.model, FLAGS.model_path, test_sess, "test")
        _, test_perplexity = run_epoch(test_sess, loaded_test_model)
        print("Test Perplexity: %.3f" % test_perplexity)
        # save softmax weights
        softmaxs = []
        softmax = train_sess.run(train_model.norm_softmax_w)
        for col in softmax.T:
            format_col = ",".join([str(_) for _ in col])
            softmaxs.append(format_col)
        with open(FLAGS.out_softmax, 'w') as f:
            for idx in range(len(softmaxs)):
                ss = str(idx) + "\t" + softmaxs[idx] + "\n"
                f.write(ss)


if __name__ == "__main__":
    tf.app.run()