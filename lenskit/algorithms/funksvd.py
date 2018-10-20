"""
FunkSVD (biased MF).
"""

import logging
import time

import pandas as pd
import numpy as np
import numba as n

from . import Trainable, Predictor
from . import basic
from .mf_common import BiasMFModel
from .. import util

_logger = logging.getLogger(__name__)


@n.jitclass([
    ('user_features', n.double[:, :]),
    ('item_features', n.double[:, :]),
    ('feature_count', n.int32),
    ('user_count', n.int32),
    ('item_count', n.int32),
    ('initial_value', n.double)
])
class Model:
    def __init__(self, umat, imat):
        self.user_features = umat
        self.item_features = imat
        self.feature_count = umat.shape[1]
        assert imat.shape[1] == self.feature_count
        self.user_count = umat.shape[0]
        self.item_count = imat.shape[0]
        self.initial_value = np.nan


def _fresh_model(nfeatures, nusers, nitems, init=0.1):
    umat = np.full([nusers, nfeatures], init, dtype=np.float_)
    imat = np.full([nitems, nfeatures], init, dtype=np.float_)
    model = Model(umat, imat)
    model.initial_value = init
    assert model.feature_count == nfeatures
    assert model.user_count == nusers
    assert model.item_count == nitems
    return model


@n.jitclass([
    ('iter_count', n.int32),
    ('learning_rate', n.double),
    ('reg_term', n.double),
    ('rmin', n.double),
    ('rmax', n.double)
])
class _Params:
    def __init__(self, niters, lrate, reg, rmin, rmax):
        self.iter_count = niters
        self.learning_rate = lrate
        self.reg_term = reg
        self.rmin = rmin
        self.rmax = rmax


def make_params(niters, lrate, reg, range):
    if range is None:
        rmin = -np.inf
        rmax = np.inf
    else:
        rmin, rmax = range

    return _Params(niters, lrate, reg, rmin, rmax)


@n.jitclass([
    ('est', n.double[:]),
    ('feature', n.int32),
    ('trail', n.double)
])
class _FeatContext:
    def __init__(self, est, feature, trail):
        self.est = est
        self.feature = feature
        self.trail = trail


@n.jitclass([
    ('users', n.int32[:]),
    ('items', n.int32[:]),
    ('ratings', n.double[:]),
    ('bias', n.double[:]),
    ('n_samples', n.uint64)
])
class Context:
    def __init__(self, users, items, ratings, bias):
        self.users = users
        self.items = items
        self.ratings = ratings
        self.bias = bias
        self.n_samples = users.shape[0]

        assert items.shape[0] == self.n_samples
        assert ratings.shape[0] == self.n_samples
        assert bias.shape[0] == self.n_samples


@n.njit
def _feature_loop(ctx: Context, params: _Params, model: Model, fc: _FeatContext):
    users = ctx.users
    items = ctx.items
    ratings = ctx.ratings
    umat = model.user_features
    imat = model.item_features
    est = fc.est
    f = fc.feature
    trail = fc.trail

    sse = 0.0
    acc_ud = 0.0
    acc_id = 0.0
    for s in range(ctx.n_samples):
        user = users[s]
        item = items[s]
        ufv = umat[user, f]
        ifv = imat[item, f]

        pred = est[s] + ufv * ifv + trail
        if pred < params.rmin:
            pred = params.rmin
        elif pred > params.rmax:
            pred = params.rmax

        error = ratings[s] - pred
        sse += error * error

        # compute deltas
        ufd = error * ifv - params.reg_term * ufv
        ufd = ufd * params.learning_rate
        acc_ud += ufd * ufd
        ifd = error * ufv - params.reg_term * ifv
        ifd = ifd * params.learning_rate
        acc_id += ifd * ifd
        umat[user, f] += ufd
        imat[item, f] += ifd

    return np.sqrt(sse / ctx.n_samples)


@n.njit
def _train_feature(ctx, params, model, fc):
    for epoch in range(params.iter_count):
        rmse = _feature_loop(ctx, params, model, fc)

    return rmse


def train(ctx: Context, params: _Params, model: Model, timer):
    est = ctx.bias

    for f in range(model.feature_count):
        start = time.perf_counter()
        trail = model.initial_value * model.initial_value * (model.feature_count - f - 1)
        fc = _FeatContext(est, f, trail)
        rmse = _train_feature(ctx, params, model, fc)
        end = time.perf_counter()
        _logger.info('[%s] finished feature %d (RMSE=%f) in %.2fs',
                     timer, f, rmse, end - start)

        est = est + model.user_features[ctx.users, f] * model.item_features[ctx.items, f]
        est = np.maximum(est, params.rmin)
        est = np.minimum(est, params.rmax)


def _align_add_bias(bias, index, keys, series):
    "Realign a bias series with an index, and add to a series"
    # realign bias to make sure it matches
    bias = bias.reindex(index, fill_value=0)
    assert len(bias) == len(index)
    # look up bias for each key
    ibs = bias.loc[keys]
    # change index
    ibs.index = keys.index
    # and add
    series = series + ibs
    return bias, series


class FunkSVD(Predictor, Trainable):
    """
    Algorithm class implementing FunkSVD matrix factorization.

    Args:
        features(int): the number of features to train
        iterations(int): the number of iterations to train each feature
        lrate(double): the learning rate
        reg(double): the regularization factor
        damping(double): damping factor for the underlying mean
        range(tuple):
            the ``(min, max)`` rating values to clamp ratings, or ``None`` to leave
            predictions unclamped.
    """

    def __init__(self, features, iterations=100, lrate=0.001, reg=0.015, damping=5, range=None):
        self.features = features
        self.iterations = iterations
        self.learning_rate = lrate
        self.regularization = reg
        self.damping = damping
        self.range = range

    def train(self, ratings, bias=None):
        """
        Train a FunkSVD model.

        Args:
            ratings: the ratings data frame.
            bias(.bias.BiasModel): a pre-trained bias model to use.

        Returns:
            The trained biased MF model.
        """
        timer = util.Stopwatch()
        if bias is None:
            _logger.info('[%s] training bias model', timer)
            bias = basic.Bias(damping=self.damping).train(ratings)
        # unpack the bias
        if not isinstance(bias, basic.BiasModel):
            bias = basic.BiasModel(bias, None, None)

        _logger.info('[%s] preparing rating data for %d samples', timer, len(ratings))
        _logger.debug('shuffling rating data')
        shuf = np.arange(len(ratings), dtype=np.int_)
        np.random.shuffle(shuf)
        ratings = ratings.iloc[shuf, :]

        _logger.debug('[%s] indexing users and items', timer)
        uidx = pd.Index(ratings.user.unique())
        iidx = pd.Index(ratings.item.unique())

        users = uidx.get_indexer(ratings.user).astype(np.int32)
        assert np.all(users >= 0)
        items = iidx.get_indexer(ratings.item).astype(np.int32)
        assert np.all(items >= 0)

        _logger.debug('[%s] computing initial estimates', timer)
        initial = pd.Series(bias.mean, index=ratings.index, dtype=np.float_)
        ibias, initial = _align_add_bias(bias.items, iidx, ratings.item, initial)
        ubias, initial = _align_add_bias(bias.users, uidx, ratings.user, initial)

        _logger.debug('have %d estimates for %d ratings', len(initial), len(ratings))
        assert len(initial) == len(ratings)

        _logger.debug('[%s] initializing data structures', timer)
        context = Context(users, items, ratings.rating.astype(np.float_).values,
                          initial.values)
        params = make_params(self.iterations, self.learning_rate, self.regularization, self.range)

        model = _fresh_model(self.features, len(uidx), len(iidx))

        _logger.info('[%s] training biased MF model with %d features', timer, self.features)
        train(context, params, model, timer)
        _logger.info('finished model training in %s', timer)

        return BiasMFModel(uidx, iidx, basic.BiasModel(bias.mean, ibias, ubias),
                           model.user_features, model.item_features)

    def predict(self, model, user, items, ratings=None):
        # look up user index
        uidx = model.lookup_user(user)
        if uidx < 0:
            _logger.debug('user %s not in model', user)
            return pd.Series(np.nan, index=items)

        # get item index & limit to valid ones
        items = np.array(items)
        iidx = model.lookup_items(items)
        good = iidx >= 0
        good_items = items[good]
        good_iidx = iidx[good]

        # multiply
        _logger.debug('scoring %d items for user %s', len(good_items), user)
        rv = model.score(uidx, good_iidx)

        # clamp if suitable
        if self.range is not None:
            rmin, rmax = self.range
            rv = np.maximum(rv, rmin)
            rv = np.minimum(rv, rmax)

        res = pd.Series(rv, index=good_items)
        res = res.reindex(items)
        return res

    def save_model(self, model, path):
        model.save(path)

    def load_model(self, path):
        return BiasMFModel.load(path)

    def __str__(self):
        return 'FunkSVD(features={}, regularization={})'.\
            format(self.features, self.regularization)
