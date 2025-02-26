import pandas as pd
from ..util import extract_user_item, sample_groupA, split_by_user
from .base import create_dataset


def prepare_ml_1m_data(data_path="data/ml-1m/ratings.dat",
    seed=0, second_half_only=True, **kw):

    event_df = _load_sort_ml_1m_data(data_path, seed)
    if second_half_only:
        event_df = event_df[
            event_df.groupby("USER_ID")["TIMESTAMP"].rank(method="first", pct=True) >= 0.5]

    user_df, item_df = extract_user_item(event_df)
    in_groupA = sample_groupA(user_df, seed=seed+888)

    test_start_rel = (user_df['_Tmax'] - user_df['_Tmin']).quantile(0.5)
    horizon = test_start_rel * 1.0
    print({"test_start_rel": test_start_rel, "horizon": horizon})

    train_df, valid_df = split_by_user(user_df, in_groupA, test_start_rel)
    D = create_dataset(event_df, train_df, item_df, horizon, **kw)
    D.print_stats()
    V = create_dataset(event_df, valid_df, item_df, horizon, **kw)
    return D, V


def _load_sort_ml_1m_data(data_path, seed):
    return pd.read_csv(
        data_path, sep="::", names=["USER_ID", "ITEM_ID", "_", "TIMESTAMP"]
    ).sample(frac=1, random_state=seed).sort_values("TIMESTAMP", kind="mergesort")
