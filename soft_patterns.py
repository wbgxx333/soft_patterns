#!/usr/bin/env python

import sys
import argparse
from collections import OrderedDict
from time import monotonic

import numpy as np
import os
import torch
from torch import FloatTensor, LongTensor, cat, mm, norm, randn, zeros, ones
from torch.autograd import Variable
from torch.nn import Module, Parameter, NLLLoss
from torch.nn.functional import sigmoid, log_softmax
from torch.optim import Adam
from torch.optim.lr_scheduler import ReduceLROnPlateau

from tensorboardX import SummaryWriter

from data import read_embeddings, read_docs, read_labels, vocab_from_text, Vocab
from mlp import MLP
from util import chunked, identity


def fixed_var(tensor, gpu=False):
    if gpu:
        return Variable(tensor, requires_grad=False).cuda()
    else:
        return Variable(tensor, requires_grad=False)


def argmax(output):
    """ only works for kxn tensors """
    _, am = torch.max(output, 1)
    return am


def get_nearest_neighbors(w, embeddings):
    dot_products = mm(w, embeddings[:1000, :])
    return argmax(dot_products)


def normalize(data):
    length = data.size()[0]
    for i in range(length):
        data[i] = data[i] / norm(data[i])  # unit length


class Semiring:
    def __init__(self,
                 zero,
                 one,
                 plus,
                 times,
                 from_float):
        self.zero = zero
        self.one = one
        self.plus = plus
        self.times = times
        self.from_float = from_float


def neg_infinity(*sizes):
    return -100 * ones(*sizes)  # not really -inf, shh


# element-wise plus, times
ProbSemiring = Semiring(zeros, ones, torch.add, torch.mul, sigmoid)

# element-wise max, plus
MaxPlusSemiring = Semiring(neg_infinity, zeros, torch.max, torch.add, identity)


class Batch:
    def __init__(self, docs, embeddings, gpu):
        """ Makes a smaller vocab of only words used in the given docs """
        mini_vocab = Vocab.from_docs(docs, default=0, pad_token=1)
        self._max_doc_size = np.max([len(doc) for doc in docs])
        self.docs = [mini_vocab.numberize(doc) for doc in docs]
        self.pad_docs(gpu)

        local_embeddings = [embeddings[i] for i in mini_vocab.names]
        self.embeddings_matrix = fixed_var(FloatTensor(local_embeddings).t(), gpu)

    def pad_docs(self, gpu):
        """Pad each document, and turn it into a variable"""
        for i in range(self.size()):
            self.docs[i] += [1] * (self.max_doc_size() - len(self.docs[i]))
            doc_trensor = torch.LongTensor(self.docs[i])
            if gpu:
                doc_trensor = doc_trensor.cuda()
            self.docs[i] = Variable(doc_trensor)




    def size(self):
        return len(self.docs)

    def max_doc_size(self):
        return self._max_doc_size


class SoftPatternClassifier(Module):
    """
    A text classification model that feeds the document scores from a bunch of
    soft patterns into an MLP
    """

    def __init__(self,
                 pattern_specs,
                 mlp_hidden_dim,
                 num_mlp_layers,
                 num_classes,
                 embeddings,
                 vocab,
                 semiring,
                 gpu=False,
                 dropout=0,
                 legacy=False):
        super(SoftPatternClassifier, self).__init__()
        self.semiring = semiring
        self.vocab = vocab
        self.embeddings = embeddings

        self.dtype = torch.FloatTensor
        if gpu:
            self.dtype = torch.cuda.FloatTensor

        self.gpu = gpu

        self.total_num_patterns = sum(pattern_specs.values())

        self.mlp = MLP(self.total_num_patterns, mlp_hidden_dim, num_mlp_layers, num_classes, legacy)

        self.word_dim = len(embeddings[0])
        self.num_diags = 2  # self-loops and single-forward-steps
        self.pattern_specs = pattern_specs
        self.max_pattern_length = max(list(pattern_specs.keys()))

        # end state index for each pattern
        end_states = [
            [end]
            for pattern_len, num_patterns in self.pattern_specs.items()
            for end in num_patterns * [pattern_len - 1]
        ]
        self.end_states = fixed_var(LongTensor(end_states))
        if self.gpu:
            self.end_states = self.end_states.cuda()

        diag_data_size = self.max_pattern_length * self.num_diags * self.total_num_patterns

        diag_data = randn(diag_data_size, self.word_dim).type(self.dtype)
        normalize(diag_data)

        # Bias term
        bias_data = randn(diag_data_size, 1).type(self.dtype)

        self.dropout = None
        if dropout:
            self.dropout = torch.nn.Dropout(dropout)

        self.diags = Parameter(diag_data)
        self.bias = Parameter(bias_data)

        self.epsilon = Parameter(randn(self.total_num_patterns, self.max_pattern_length - 1).type(self.dtype))

        # TODO: learned? hyperparameter?
        self.epsilon_scale = fixed_var(FloatTensor([0]).type(self.dtype))
        self.self_loop_scale = fixed_var(FloatTensor([0]).type(self.dtype))
        print("# params:", sum(p.nelement() for p in self.parameters()))

    def visualize_pattern(self, batch_size, dev_set=None, dev_text=None, n_top_scoring=5):
        nearest_neighbors = get_nearest_neighbors(self.diags.data, FloatTensor(self.embeddings).t())

        if dev_set is not None:
            # print(dev_set[0])
            scores = self.get_top_scoring_sequences(dev_set, batch_size)

        start = 0
        for i, (pattern_length, num_patterns) in enumerate(self.pattern_specs.items()):
            # 1 above main diagonal
            viewed_tensor = \
                self.diags[self.starts[i]:self.ends[i], :].view(
                    num_patterns,
                    self.num_diags,
                    pattern_length,
                    self.word_dim
                )[:, 1, :-1, :]
            norms = norm(viewed_tensor, 2, 2)
            viewed_biases = \
                self.bias[self.starts[i]:self.ends[i], :].view(
                    num_patterns,
                    self.num_diags,
                    pattern_length
                )[:, 1, :-1]
            reviewed_nearest_neighbors = \
                nearest_neighbors[self.starts[i]:self.ends[i]].view(
                    num_patterns,
                    self.num_diags,
                    pattern_length
                )[:, 1, :-1]

            if dev_set is not None:
                for p in range(num_patterns):
                    patt_scores = scores[start + p, :, :]
                    last_n = len(patt_scores) - n_top_scoring
                    sorted_keys = sorted(range(len(patt_scores)), key=lambda i: patt_scores[i][0].data[0])

                    print("Top scoring",
                          [(" ".join(dev_text[k][int(patt_scores[k][1].data[0]):int(patt_scores[k][2].data[0])]),
                            round(patt_scores[k][0].data[0], 3)) for k in sorted_keys[last_n:]],
                          "norms", [round(x, 3) for x in norms.data[p, :]],
                          'biases', [round(x, 3) for x in viewed_biases.data[p, :]],
                          'nearest neighbors', [self.vocab[x] for x in reviewed_nearest_neighbors[p, :]])
                start += num_patterns

    def get_top_scoring_sequences(self, dev_set, batch_size):
        """
        Get top scoring sequence in doc for this pattern (for interpretation purposes)
        """
        n = 3  # max_score, start_idx, end_idx

        max_scores = Variable(MaxPlusSemiring.zero(self.total_num_patterns, len(dev_set), n))

        zero_paddings = [
            fixed_var(self.semiring.zero(num_patterns, 1), self.gpu)
            for num_patterns in self.pattern_specs.values()
        ]

        debug_print = int(100 / batch_size) + 1
        eps_value = self.get_eps_value()
        self_loop_scale = self.get_self_loop_scale()

        i = 0
        for batch_idx, batch in enumerate(chunked(dev_set, batch_size)):
            if i % debug_print == (debug_print - 1):
                print(".", end="", flush=True)
            i += 1
            batch_obj = Batch([x for x, y in batch], self.embeddings, self.gpu)

            transition_matrices = self.get_transition_matrices(batch_obj)

            for d in range(batch_obj.size()):
                doc = batch_obj.docs[d]
                doc_idx = batch_idx * batch_size + d
                for i in range(len(doc)):
                    start = 0
                    for k, (pattern_length, num_patterns) in enumerate(self.pattern_specs.items()):
                        hiddens = Variable(self.semiring.zero(num_patterns, pattern_length).type(self.dtype))

                        # Start state
                        hiddens[:, 0] = self.semiring.one(num_patterns, 1).type(self.dtype)

                        for j in range(i, min(i + pattern_length - 1, len(doc))):
                            transition_matrix_val = transition_matrices[d][j][k]
                            hiddens = self.transition_once(
                                eps_value,
                                hiddens,
                                self_loop_scale,
                                transition_matrix_val,
                                zero_paddings[k],
                                zero_paddings[k])

                            scores = hiddens[:, -1]

                            for p in range(num_patterns):
                                pattern_idx = start + p
                                if scores[p].data[0] > max_scores[pattern_idx, doc_idx, 0].data[0]:
                                    max_scores[pattern_idx, doc_idx, 0] = scores[p]
                                    max_scores[pattern_idx, doc_idx, 1] = i
                                    max_scores[pattern_idx, doc_idx, 2] = j + 1
                        start += num_patterns
        print()
        return max_scores

    def get_transition_matrices(self, batch):
        mm_res = mm(self.diags, batch.embeddings_matrix)
        transition_scores = \
            self.semiring.from_float(mm_res + self.bias.expand(self.bias.size()[0], mm_res.size()[1])).t()

        if self.gpu:
            transition_scores = transition_scores.cuda()

        if self.dropout:
            transition_scores = self.dropout(transition_scores)

        batched_transition_scores = [
            torch.index_select(transition_scores, 0, doc) for doc in batch.docs
        ]

        batched_transition_scores = torch.cat(batched_transition_scores).view(
            batch.size(), int(batch.max_doc_size()), self.total_num_patterns, self.num_diags, self.max_pattern_length)

        # transition matrix for each document in batch
        transition_matrices = [
            batched_transition_scores[:, word_index, :, :, :]
            for word_index in range(batch.max_doc_size())
        ]

        return transition_matrices

    def forward(self, batch, debug=None):
        """
        Calculate score for one document.
        doc -- a sequence of indices that correspond to the word embedding matrix
        """
        time1 = monotonic()
        transition_matrices = self.get_transition_matrices(batch)
        time2 = monotonic()

        scores = Variable(self.semiring.zero(batch.size(), self.total_num_patterns).type(self.dtype))

        # to add start state for each word in the document.
        restart_padding = fixed_var(self.semiring.one(batch.size(), self.total_num_patterns, 1), self.gpu)

        zero_padding = fixed_var(self.semiring.zero(batch.size(), self.total_num_patterns, 1), self.gpu)

        eps_value = \
            self.semiring.times(
                self.semiring.from_float(self.epsilon_scale),
                self.semiring.from_float(self.epsilon)
            )

        eps_value = eps_value.expand(batch.size(), eps_value.size()[0], eps_value.size()[1])
        self_loop_scale = self.get_self_loop_scale()
        end_state_local = self.end_states.expand(batch.size(), self.total_num_patterns, 1)
        # Different documents in batch
        hiddens = Variable(self.semiring.zero(batch.size(), self.total_num_patterns, self.max_pattern_length).type(self.dtype))
        hiddens[:, :, 0] = self.semiring.one(batch.size(), self.total_num_patterns, 1).type(self.dtype)
            # Start state
            # For each token in document

        for i in range (batch.max_doc_size()):
            transition_matrix_val = transition_matrices[i]
            hiddens = self.transition_once(eps_value,
                                           hiddens,
                                           self_loop_scale,
                                           transition_matrix_val,
                                           zero_padding,
                                           restart_padding)

            # Look at the end state for each pattern, and "add" it into score
            end_state_vals = torch.gather(hiddens, 2, end_state_local).view(scores.size()[0], scores.size()[1])
            # print(end_state_vals.size(), scores.size())
            scores = \
                self.semiring.plus(
                    scores,
                    end_state_vals
                )

        if debug:
            time3 = monotonic()
            print("MM: {}, other: {}".format(round(time2 - time1, 3), round(time3 - time2, 3)))
        return self.mlp.forward(scores)

    def get_self_loop_scale(self):
        return self.semiring.from_float(self.self_loop_scale)

    def get_eps_value(self):
        return self.semiring.times(
            self.semiring.from_float(self.epsilon_scale),
            self.semiring.from_float(self.epsilon)
        )

    def transition_once(self,
                        eps_value,
                        hiddens,
                        self_loop_scale,
                        transition_matrix_val,
                        zero_padding,
                        restart_padding):
        # Adding epsilon transitions (don't consume a token, move forward one state)
        # We do this before self-loops and single-steps.
        # We only allow one epsilon transition in a row.
        hiddens = \
            self.semiring.plus(
                hiddens,
                cat((zero_padding,
                     self.semiring.times(
                         hiddens[:, :, :-1],
                         eps_value  # doesn't depend on token, just state
                     )), 2))
        # single steps forward (consume a token, move forward one state)
        result = \
            cat((restart_padding,  # <- Adding the start state
                 self.semiring.times(
                     hiddens[:, :, -1].contiguous().view(hiddens.size()[0], hiddens.size()[1], 1),
                     transition_matrix_val[:, :, 1, :-1])
                 ), 2)
        # Adding self loops (consume a token, stay in same state)
        result = \
            self.semiring.plus(
                result,
                self.semiring.times(
                    self_loop_scale,
                    self.semiring.times(
                        hiddens,
                        transition_matrix_val[:, :, 0, :]
                    )
                )
            )
        return result

    def predict(self, batch, debug=None):
        output = self.forward(batch, debug).data
        return [int(x) for x in argmax(output)]


def train_batch(model, batch, num_classes, gold_output, optimizer, loss_function, gpu=False, clip=None, debug=None):
    """Train on one doc. """
    optimizer.zero_grad()
    time0 = monotonic()
    loss = compute_loss(model, batch, num_classes, gold_output, loss_function, gpu, debug)
    # print("ls", loss.size())

    time1 = monotonic()
    loss.backward()

    time2 = monotonic()
    if clip is not None:
        torch.nn.utils.clip_grad_norm(model.parameters(), clip)

    optimizer.step()

    if debug:
        time3 = monotonic()
        print("Time in loss: {}, time in backword: {}, time in step: {}".format(round(time1 - time0, 3),
                                                                                round(time2 - time1, 3),
                                                                                round(time3 - time2, 3)))

    return loss.data


def compute_loss(model, batch, num_classes, gold_output, loss_function, gpu, debug=None):
    time1 = monotonic()
    output = model.forward(batch, debug)

    if debug:
        time2 = monotonic()
        print("Forward total in loss: {}".format(round(time2 - time1, 3)))

    # print("os", output.dim(), output.size(), "bs", batch.size(), "gs", len(gold_output))
    return loss_function(
        log_softmax(output).view(batch.size(), num_classes),
        fixed_var(LongTensor(gold_output), gpu)
    )


def evaluate_accuracy(model, data, batch_size, gpu, debug=None):
    n = float(len(data))
    correct = 0
    num_1s = 0
    for batch in chunked(data, batch_size):
        batch_obj = Batch([x[0] for x in batch], model.embeddings, gpu)
        gold = [x[1] for x in batch]
        predicted = model.predict(batch_obj, debug)
        num_1s += sum(predicted)
        correct += sum(1 for pred, gold in zip(predicted, gold) if pred == gold)

    print("num predicted 1s", num_1s)
    print("num gold 1s", sum(gold for _, gold in data))

    return correct / n


def train(train_data,
          dev_data,
          model,
          num_classes,
          model_save_dir,
          num_iterations,
          model_file_prefix,
          learning_rate,
          batch_size,
          run_scheduler=False,
          gpu=False,
          clip=None,
          debug=None):
    """ Train a model on all the given docs """
    optimizer = Adam(model.parameters(), lr=learning_rate)
    loss_function = NLLLoss(None, False)

    debug_print = int(100 / batch_size) + 1

    writer = None

    if model_save_dir is not None:
        writer = SummaryWriter(os.path.join(model_save_dir, "logs"))

    if run_scheduler:
        scheduler = ReduceLROnPlateau(optimizer, 'min', 0.1, 10, True)

    start_time = monotonic()

    for it in range(num_iterations):
        np.random.shuffle(train_data)

        loss = 0.0
        i = 0
        for batch in chunked(train_data, batch_size):
            batch_obj = Batch([x[0] for x in batch], model.embeddings, gpu)
            gold = [x[1] for x in batch]
            loss += torch.sum(
                train_batch(model, batch_obj, num_classes, gold, optimizer, loss_function, gpu, clip, debug)
            )
            if i % debug_print == (debug_print - 1):
                print(".", end="", flush=True)
                if writer is not None:
                    for name, param in model.named_parameters():
                        writer.add_scalar("parameter_mean/" + name,
                                          param.data.mean(),
                                          i)
                        writer.add_scalar("parameter_std/" + name, param.data.std(), i)
                        if param.grad is not None:
                            writer.add_scalar("gradient_mean/" + name,
                                              param.grad.data.mean(),
                                              i)
                            writer.add_scalar("gradient_std/" + name,
                                              param.grad.data.std(),
                                              i)
                    writer.add_scalar("loss/loss_train", loss, i)

            i += 1

        dev_loss = 0.0
        i = 0
        for batch in chunked(dev_data, batch_size):
            batch_obj = Batch([x[0] for x in batch], model.embeddings, gpu)
            gold = [x[1] for x in batch]
            dev_loss += torch.sum(compute_loss(model, batch_obj, num_classes, gold, loss_function, gpu, debug).data)
            if i % debug_print == (debug_print - 1):
                print(".", end="", flush=True)

                if writer is not None:
                    writer.add_scalar("loss/loss_dev", dev_loss, i)

            i += 1

        print("\n")
        finish_iter_time = monotonic()
        train_acc = evaluate_accuracy(model, train_data, batch_size, gpu)
        dev_acc = evaluate_accuracy(model, dev_data, batch_size, gpu)

        print(
            "iteration: {:>7,} train time: {:>9,.3f}m, eval time: {:>9,.3f}m "
            "train loss: {:>12,.3f} train_acc: {:>8,.3f}% "
            "dev loss: {:>12,.3f} dev_acc: {:>8,.3f}%".format(
                it,
                (finish_iter_time - start_time) / 60,
                (monotonic() - finish_iter_time) / 60,
                loss / len(train_data),
                train_acc * 100,
                dev_loss / len(dev_data),
                dev_acc * 100
            )
        )

        if run_scheduler:
            scheduler.step(dev_loss)

        if model_save_dir is not None:
            model_save_file = os.path.join(model_save_dir, "{}_{}.pth".format(model_file_prefix, it))
            torch.save(model.state_dict(), model_save_file)

    return model


def main(args):
    print(args)
    pattern_specs = OrderedDict([int(y) for y in x.split(":")] for x in args.patterns.split(","))
    n = args.num_train_instances
    mlp_hidden_dim = args.mlp_hidden_dim
    num_mlp_layers = args.num_mlp_layers

    if args.seed != -1:
        torch.manual_seed(args.seed)
        np.random.seed(args.seed)

    dev_vocab = vocab_from_text(args.vd)
    print("Dev vocab:", len(dev_vocab))
    if args.td is not None:
        train_vocab = vocab_from_text(args.td)
        print("Train vocab:", len(train_vocab))
        dev_vocab |= train_vocab

    vocab, embeddings, word_dim = \
        read_embeddings(args.embedding_file, dev_vocab)

    dev_input, dev_text = read_docs(args.vd, vocab)
    dev_labels = read_labels(args.vl)
    dev_data = list(zip(dev_input, dev_labels))

    if args.td is not None:
        if args.tl is None:
            print("Both training data (--td) and training labels (--tl) required in training mode")
            return -1

        np.random.shuffle(dev_data)
        num_iterations = args.num_iterations

        train_input, _ = read_docs(args.td, vocab)
        train_labels = read_labels(args.tl)

        print("training instances:", len(train_input))

        num_classes = len(set(train_labels))

        # truncate data (to debug faster)
        train_data = list(zip(train_input, train_labels))
        np.random.shuffle(train_data)
    else:
        num_classes = len(set(dev_labels))

    print("num_classes:", num_classes)

    if n is not None:
        if args.td is not None:
            train_data = train_data[:n]

        dev_data = dev_data[:n]

    dropout = None if args.td is None else args.dropout
    semiring = MaxPlusSemiring if args.maxplus else ProbSemiring

    model = SoftPatternClassifier(pattern_specs,
                                  mlp_hidden_dim,
                                  num_mlp_layers,
                                  num_classes,
                                  embeddings,
                                  vocab,
                                  semiring,
                                  args.gpu,
                                  dropout,
                                  args.legacy)

    if args.gpu:
        model.cuda()

    model_file_prefix = 'model'
    # Loading model
    if args.input_model is not None:
        state_dict = torch.load(args.input_model)
        model.load_state_dict(state_dict)
        model_file_prefix = 'model_retrained'

    if args.td:
        model_save_dir = args.model_save_dir

        if model_save_dir is not None:
            if not os.path.exists(model_save_dir):
                os.makedirs(model_save_dir)

        print("Training with", model_file_prefix)
        train(train_data,
              dev_data,
              model,
              num_classes,
              model_save_dir,
              num_iterations,
              model_file_prefix,
              args.learning_rate,
              args.batch_size,
              args.scheduler,
              args.gpu,
              args.clip,
              args.debug)
    else:
        model.visualize_pattern(args.batch_size, dev_data, dev_text)

    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument("-e", "--embedding_file", help="Word embedding file", required=True)
    parser.add_argument("-s", "--seed", help="Random seed", type=int, default=100)
    parser.add_argument("-i", "--num_iterations", help="Number of iterations", type=int, default=10)
    parser.add_argument("-p", "--patterns",
                        help="Pattern lengths and numbers: a comma separated list of length:number pairs",
                        default="5:50,4:50,3:50,2:50")
    parser.add_argument("-d", "--mlp_hidden_dim", help="MLP hidden dimension", type=int, default=10)
    parser.add_argument("-b", "--batch_size", help="Batch size", type=int, default=1)
    parser.add_argument("-y", "--num_mlp_layers", help="Number of MLP layers", type=int, default=2)
    parser.add_argument("-n", "--num_train_instances", help="Number of training instances", type=int, default=None)
    parser.add_argument("-m", "--model_save_dir", help="where to save the trained model")
    parser.add_argument("-r", "--scheduler", help="Use reduce learning rate on plateau schedule", action='store_true')
    parser.add_argument("-g", "--gpu", help="Use GPU", action='store_true')
    parser.add_argument("-c", "--legacy", help="Load legacy models", action='store_true')
    parser.add_argument("-t", "--dropout", help="Use dropout", type=float, default=0)
    parser.add_argument("--input_model", help="Input model (to run test and not train)")
    parser.add_argument("--td", help="Train data file")
    parser.add_argument("--tl", help="Train labels file")
    parser.add_argument("--vd", help="Validation data file", required=True)
    parser.add_argument("--vl", help="Validation labels file", required=True)
    parser.add_argument("-l", "--learning_rate", help="Adam Learning rate", type=float, default=1e-3)
    parser.add_argument("--clip", help="Gradient clipping", type=float, default=None)
    parser.add_argument("--debug", help="Debug", action='store_true')
    parser.add_argument("--maxplus",
                        help="Use max-plus semiring instead of plus-times",
                        default=False, action='store_true')

    sys.exit(main(parser.parse_args()))
