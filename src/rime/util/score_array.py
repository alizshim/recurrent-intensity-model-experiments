import numpy as np, pandas as pd
import torch, dataclasses, functools, warnings, operator, builtins
from typing import Dict, List
from torch.utils.data import DataLoader
import scipy.sparse as sps

def get_batch_size(shape, frac=0.1):
    """ round to similar batch sizes """
    n_users, n_items = shape
    if torch.cuda.device_count():
        total_memory = torch.cuda.get_device_properties(0).total_memory
    else:
        total_memory = 16e9
    max_batch_size = total_memory / 8 / n_items * frac
    n_batches = int(n_users / max_batch_size) + 1
    return int(np.ceil(n_users / n_batches))

def df_to_coo(df):
    """ fix pandas bug: https://github.com/pandas-dev/pandas/issues/25270 """
    try:
        return df.sparse.to_coo()
    except KeyError:
        df = df.copy()
        df.index = list(range(len(df.index)))
        df.columns = list(range(len(df.columns)))
        return df.sparse.to_coo()

def pd_sparse_reindex(df, index, axis, fill_value=0):
    """ fix pandas bug: reindex silently drops sparsity when the index length > 36 """
    if axis==1:
        return pd_sparse_reindex(df.T, index, 0, fill_value).T.copy()
    csr = df_to_coo(df).tocsr().copy()
    csr.resize((csr.shape[0]+1, csr.shape[1]))
    csr[-1, :] = fill_value
    csr.eliminate_zeros()
    new_ind = pd.Series(
            np.arange(df.shape[0]), index=df.index
            ).reindex(index, fill_value=-1).values
    return pd.DataFrame.sparse.from_spmatrix(csr[new_ind], index, df.columns)

def sps_to_torch(x, device):
    """ convert scipy.sparse to torch.sparse """
    coo = x.tocoo()
    values = coo.data
    indices = np.vstack((coo.row, coo.col))
    return torch.sparse_coo_tensor(indices, values, coo.shape, device=device)

def _auto_eval(c, device):
    """ support LazyScoreExpression, scalar, scipy.sparse, 2d array """
    assert not isinstance(c, pd.DataFrame), "please call values property first"
    if isinstance(c, LazyScoreExpression):
        return c.eval(device)
    elif np.isscalar(c):
        return c
    elif sps.issparse(c):
        return c.toarray() if device is None else sps_to_torch(c, device).to_dense()
    elif np.ndim(c) == 2:
        return np.asarray(c) if device is None else torch.as_tensor(c, device=device)
    else:
        raise NotImplementedError(str(c))

def _auto_values(c):
    """ support LazyScoreExpression, scalar, pd.DataFrame """
    if isinstance(c, LazyScoreExpression):
        return c.values
    elif np.isscalar(c):
        return c
    elif isinstance(c, pd.DataFrame):
        return df_to_coo(c).tocsr() if hasattr(c, 'sparse') else c.values
    else:
        raise NotImplementedError(str(c))

def _auto_getitem(c, key):
    """ support LazyScoreExpression, scalar, scipy.sparse, 2d array """
    assert not isinstance(c, pd.DataFrame), "please call values property first"
    if np.isscalar(c):
        return c
    else:
        return c[key]

def _auto_collate(c, D):
    """ support LazyScoreExpression, scalar, scipy.sparse, 2d array """
    assert not isinstance(c, pd.DataFrame), "please call values property first"
    if isinstance(c, LazyScoreExpression):
        return c.collate_fn(D)
    elif np.isscalar(c):
        return D[0]
    elif sps.issparse(c):
        return sps.vstack(D)
    elif np.ndim(c) == 2:
        return np.vstack(D)
    else:
        raise NotImplementedError(str(c))


class LazyScoreExpression:
    """ Base class is automatically created after a binary operation between its derived
    subclass and a compatible matrix / scalar.
    """
    def __init__(self, op, children):
        self.op = op
        self.children = children
        self.index = children[0].index
        self.columns = children[0].columns
        self.shape = children[0].shape

    def eval(self, device=None):
        children = [_auto_eval(c, device) for c in self.children]
        return self.op(*children)

    @property
    def values(self):
        children = [_auto_values(c) for c in self.children]
        return self.__class__(self.op, children)

    def __len__(self):
        return self.shape[0]

    @property
    def size(self):
        return np.prod(self.shape)

    @property
    def batch_size(self):
        return get_batch_size(self.shape)

    def _check_index_columns(self, other):
        if not np.isscalar(other):
            assert np.allclose(self.shape, other.shape), "shape must be compatible"

    def __add__(self, other):
        self._check_index_columns(other)
        return LazyScoreExpression(operator.add, [self, other])

    def __mul__(self, other):
        self._check_index_columns(other)
        return LazyScoreExpression(operator.mul, [self, other])

    def clip(self, min, max):
        return LazyScoreExpression(lambda x: x.clip(min, max), [self])

    @property
    def T(self):
        children = [c if np.isscalar(c) else c.T for c in self.children]
        return self.__class__(self.op, children)

    def __getitem__(self, key):
        """ used in pytorch dataloader. ignores index / columns """
        if np.isscalar(key):
            key = [key]
        children = [_auto_getitem(c, key) for c in self.children]
        return self.__class__(self.op, children)

    @classmethod
    def collate_fn(cls, batch):
        first = batch[0]
        op = first.op
        data = zip(*[b.children for b in batch])
        children = [_auto_collate(c, D) for c, D in zip(first.children, data)]
        return cls(op, children)


@dataclasses.dataclass(repr=False)
class LowRankDataFrame(LazyScoreExpression):
    """ mimics a pandas dataframe with exponentiated low-rank structures
    """
    ind_logits: List[list]
    col_logits: List[list]
    index: list
    columns: list
    act: str

    def __post_init__(self):
        assert self.ind_logits.shape[1] == self.col_logits.shape[1], "check hidden"
        assert self.ind_logits.shape[0] == len(self.index), "check index"
        assert self.col_logits.shape[0] == len(self.columns), "check columns"
        assert self.act in ['exp', 'sigmoid'], "requires nonnegative act to solve cvx"

    def eval(self, device=None):
        if device is None:
            z = self.ind_logits @ self.col_logits.T

            if self.act == 'exp':
                return np.exp(z)
            elif self.act == 'sigmoid':
                return 1./(1+np.exp(-z))
        else:
            ind_logits = torch.as_tensor(self.ind_logits, device=device)
            col_logits = torch.as_tensor(self.col_logits, device=device)
            z = ind_logits @ col_logits.T

            if self.act == 'exp':
                return z.exp()
            elif self.act == 'sigmoid':
                return z.sigmoid()
    @property
    def values(self):
        return self

    @property
    def shape(self):
        return (len(self.ind_logits), len(self.col_logits))

    def __getitem__(self, key):
        if np.isscalar(key):
            key = [key]
        return self.__class__(self.ind_logits[key], self.col_logits,
            self.index[key], self.columns, self.act)

    @property
    def T(self):
        return self.__class__(self.col_logits, self.ind_logits,
            self.columns, self.index, self.act)

    @classmethod
    def collate_fn(cls, batch):
        ind_logits = []
        col_logits = batch[0].col_logits
        index = []
        columns = batch[0].columns
        act = batch[0].act

        for elm in batch:
            ind_logits.append(elm.ind_logits)
            index.extend(elm.index)

        return cls(np.vstack(ind_logits), col_logits, index, columns, act)

    def reindex(self, index, axis=0, fill_value=float("nan")):
        if axis==1:
            return self.T.reindex(index, fill_value=fill_value).T

        ind_logits = np.pad(self.ind_logits, ((0,1), (0,1)), constant_values=0)
        with np.errstate(divide='ignore'): # 0 -> -inf
            ind_logits[-1, -1] = np.log(fill_value)
        col_logits = np.pad(self.col_logits, ((0,0), (0,1)), constant_values=1)

        new_ind = pd.Series(
            np.arange(len(self)), index=self.index
            ).reindex(index, fill_value=-1).values

        return self.__class__(
            ind_logits[new_ind], col_logits, index, self.columns, self.act)


def score_op(S, op, device=None):
    """ aggregation operations (e.g., max, min) across entire matrix """
    out = None
    for batch in DataLoader(S, S.batch_size, collate_fn=S.collate_fn):
        val = batch.eval(device)
        new = getattr(val, op)()
        out = new if out is None else getattr(builtins, op)(out, new)
    return out
