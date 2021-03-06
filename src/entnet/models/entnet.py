from __future__ import absolute_import
from __future__ import division

import numpy as np
import tensorflow as tf
from six.moves import range

from functools import partial

from .activations import prelu
from .dynamic_memory_cell import DynamicMemoryCell
from .model_utils import get_sequence_length


class EntNetDialog(object):
    """Recurrent Entity Network."""
    def __init__(self, batch_size, vocab_size, memory_size, candidates_size, sentence_size, num_blocks, embedding_size,
                 candidates_vec,
                 clip_gradients=40.0,
                 learning_rate_init=1e-2,
                 learning_rate_decay_rate=0.5,
                 learning_rate_decay_steps=25,
                 session=tf.Session(),
                 name='EntNet'):
        """Creates an Recurrent Entity Network
    
        Args:
            batch_size: The size of the batch.
    
            vocab_size: The size of the vocabulary (should include the nil word). The nil word
            one-hot encoding should be 0.
            
            memory_size: Max number of story per example.
    
            sentence_size: The max size of a sentence in the data. All sentences should be padded
            to this length. If padding is required it should be done with nil one-hot encoding (0).
    
            candidates_size: The size of candidates
    
            num_blocks: Number of memory blocks.
    
            embedding_size: The size of the word embedding.
    
            candidates_vec: The numpy array of candidates encoding.
            
            clip_gradients: Clip the global norm of the gradients to this value. Defaults to 40.0.
            
            learning_rate_init: Base learning rate. Defaults to 1e-2.
            
            learning_rate_decay_rate: Learning rate decay rate. Default to 0.5.
    
            session: Tensorflow Session the model is run with. Defaults to `tf.Session()`.
    
            name: Name of the Recurrent Entity Network. Defaults to `EntNet`.
        """

        self._batch_size = batch_size
        self._vocab_size = vocab_size
        self._memory_size = memory_size
        self._candidates_size = candidates_size
        self._sentence_size = sentence_size
        self._num_blocks = num_blocks
        self._embedding_size = embedding_size
        self._candidates = candidates_vec
        self._one_init = tf.constant_initializer(1.0)
        self._normal_init = tf.random_normal_initializer(stddev=0.1)
        self._activation = partial(prelu, initializer=self._one_init)
        self._clip_gradients = clip_gradients
        self._learning_rate_init = learning_rate_init
        self._learning_rate_decay_rate = learning_rate_decay_rate
        self._learning_rate_decay_steps = learning_rate_decay_steps
        self._name = name
        self.saver = None

        self._build_inputs()
        self._build_vars()

        # loss op
        logits = self._inference(self._stories, self._queries) # (batch_size, candidates_size)
        loss_op = self.get_loss(logits)

        # train op
        train_op = self.get_train_op(loss_op)

        # predict ops
        predict_op = tf.argmax(logits, 1, name="predict_op")
        predict_proba_op = tf.nn.softmax(logits, name="predict_proba_op")
        predict_log_proba_op = tf.log(predict_proba_op, name="predict_log_proba_op")

        # assign ops
        self.loss_op = loss_op
        self.predict_op = predict_op
        self.predict_proba_op = predict_proba_op
        self.predict_log_proba_op = predict_log_proba_op
        self.train_op = train_op

        init_op = tf.global_variables_initializer()
        self._sess = session
        self._summary_writer = tf.summary.FileWriter('./tf_log', self._sess.graph)
        self._sess.run(init_op)
        self.saver = tf.train.Saver(max_to_keep=1)

    def _build_inputs(self):
        self._stories = tf.placeholder(tf.int32, [None, None, self._sentence_size], name="stories")
        self._queries = tf.placeholder(tf.int32, [None, 1, self._sentence_size], name="queries")
        self._answers = tf.placeholder(tf.int32, [None], name="answers")

    def _build_vars(self):
        with tf.variable_scope(self._name, initializer=self._normal_init):
            # Embeddings
            # The embedding mask forces the special "pad" embedding to zeros.
            input_embedding = tf.get_variable('input_embedding', [self._vocab_size, self._embedding_size])
            input_embedding_mask = tf.constant([0 if i == 0 else 1 for i in range(self._vocab_size)], dtype=tf.float32,
                                               shape=[self._vocab_size, 1])
            self.input_embedding_masked = input_embedding * input_embedding_mask
            output_embedding = tf.get_variable('output_embedding', [self._vocab_size, self._embedding_size])
            output_embedding_mask = tf.constant([0 if i == 0 else 1 for i in range(self._vocab_size)], dtype=tf.float32,
                                                shape=[self._vocab_size, 1])
            self.output_embedding_masked = output_embedding * output_embedding_mask

    def _inference(self, stories, queries):
        # stories = tf.Print(stories, [stories], message="Story: ")
        # print(stories.get_shape())
        # print(queries.get_shape())
        with tf.variable_scope(self._name, initializer=self._normal_init):
            story_embedding = tf.nn.embedding_lookup(self.input_embedding_masked, stories)
            query_embedding = tf.nn.embedding_lookup(self.input_embedding_masked, queries)
            # print(story_embedding.get_shape())
            # print(query_embedding.get_shape())

            # Input Module
            encoded_story = self.get_input_encoding(story_embedding, 'StoryEncoding')
            encoded_query = self.get_input_encoding(query_embedding, 'QueryEncoding')
            # print(encoded_story.get_shape())
            # print(encoded_query.get_shape())

            # Memory Module
            # We define the keys outside of the cell so they may be used for state initialization.
            keys = [tf.get_variable('key_{}'.format(j), [self._embedding_size]) for j in range(self._num_blocks)]

            sequence_length = get_sequence_length(encoded_query)
            cell = DynamicMemoryCell(self._num_blocks, self._embedding_size, keys,
                                     initializer=self._normal_init,
                                     activation=self._activation)

            # Recurrence
            initial_state = cell.zero_state(self._batch_size, tf.float32)
            # encoded_story = tf.Print(encoded_story, [encoded_story], message="Encoded story: ")
            _, last_state = tf.nn.dynamic_rnn(cell, encoded_story,
                                              sequence_length=sequence_length,
                                              initial_state=initial_state)

            # Output Module
            output = self.get_output(last_state, encoded_query)

            tf.summary.tensor_summary('sequence_length', sequence_length)
            tf.summary.tensor_summary('encoded_story', encoded_story)
            tf.summary.tensor_summary('encoded_query', encoded_query)
            tf.summary.tensor_summary('last_state', last_state)
            tf.summary.tensor_summary('output', output)

            return output

    def get_input_encoding(self, embedding, scope=None):
        with tf.variable_scope(scope, 'Encoding', initializer=tf.constant_initializer(1.0)):
            _, _, max_sentence_length, _ = embedding.get_shape().as_list()
            positional_mask = tf.get_variable('positional_mask', [max_sentence_length, 1])
            encoded_input = tf.reduce_sum(embedding * positional_mask, reduction_indices=[2])
            return encoded_input

    def get_output(self, last_state, encoded_query, scope=None):
        """
        Implementation of Section 2.3, Equation 6. This module is also described in more detail here:
        [End-To-End Memory Networks](https://arxiv.org/abs/1502.01852).
        """
        with tf.variable_scope(scope, 'Output', initializer=self._normal_init):
            last_state = tf.stack(tf.split(last_state, self._num_blocks, 1), axis=1)

            # Use the encoded_query to attend over memories (hidden states of dynamic last_state cell blocks)
            attention = tf.reduce_sum(last_state * encoded_query, reduction_indices=[2])

            # Subtract max for numerical stability (softmax is shift invariant)
            attention_max = tf.reduce_max(attention, reduction_indices=[-1], keep_dims=True)
            attention = tf.nn.softmax(attention - attention_max)
            attention = tf.expand_dims(attention, 2)

            # Weight memories by attention vectors
            u = tf.reduce_sum(last_state * attention, reduction_indices=[1])

            H = tf.get_variable('H', [self._embedding_size, self._embedding_size])

            q = tf.squeeze(encoded_query, squeeze_dims=[1])

            candidates_emb = tf.nn.embedding_lookup(self.output_embedding_masked, self._candidates)
            candidates_emb_sum = tf.reduce_sum(candidates_emb, 1)

            y = tf.matmul(self._activation(q + tf.matmul(u, H)), tf.transpose(candidates_emb_sum))
            return y

    def get_loss(self, output):
        cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=output, labels=self._answers,
                                                                       name="cross_entropy")
        cross_entropy_mean = tf.reduce_mean(cross_entropy, name="cross_entropy_mean")
        return cross_entropy_mean

    def get_train_op(self, loss):
        global_step = tf.contrib.framework.get_or_create_global_step()

        learning_rate = tf.train.exponential_decay(
            learning_rate=self._learning_rate_init,
            decay_steps=self._learning_rate_decay_steps,
            decay_rate=self._learning_rate_decay_rate,
            global_step=global_step,
            staircase=True)

        train_op = tf.contrib.layers.optimize_loss(loss, global_step=global_step, learning_rate=learning_rate,
                                                   optimizer='Adam', clip_gradients=self._clip_gradients)
        # train_op = tf.train.AdamOptimizer(learning_rate=1e-3, epsilon=1e-8).minimize(loss)
        tf.summary.scalar('loss', loss)
        tf.summary.scalar('learning_rate', learning_rate)
        return train_op

    def batch_fit(self, stories, queries, answers):
        """Runs the training algorithm over the passed batch

        Args:
            stories: Tensor (None, memory_size, sentence_size)
            queries: Tensor (None, sentence_size)
            answers: Tensor (None, vocab_size)

        Returns:
            loss: floating-point number, the loss computed for the batch
        """
        self._batch_size = len(stories)
        # flatten_stories = np.asarray(stories).reshape(self._batch_size, -1)
        expanded_queries = np.expand_dims(queries, axis=1)
        # feed_dict = {self._stories: flatten_stories, self._queries: expanded_queries, self._answers: answers}
        feed_dict = {self._stories: stories, self._queries: expanded_queries, self._answers: answers}
        merged = tf.summary.merge_all()
        loss, _, summary = self._sess.run([self.loss_op, self.train_op, merged], feed_dict=feed_dict)
        self._summary_writer.add_summary(summary, global_step=6024)
        return loss

    def predict(self, stories, queries):
        """Predicts answers as one-hot encoding.

        Args:
            stories: Tensor (None, memory_size, sentence_size)
            queries: Tensor (None, sentence_size)

        Returns:
            answers: Tensor (None, vocab_size)
        """
        self._batch_size = len(stories)
        # flatten_stories = np.asarray(stories).reshape(self._batch_size, -1)
        expanded_queries = np.expand_dims(queries, axis=1)
        # feed_dict = {self._stories: flatten_stories, self._queries: expanded_queries}
        feed_dict = {self._stories: stories, self._queries: expanded_queries}
        return self._sess.run(self.predict_op, feed_dict=feed_dict)
