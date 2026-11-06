"""Dataset.create_view makes a private view unless told otherwise (2.0), as client.create_view already did."""

from fridge.core.dataset import Dataset

STAGES = [{'type': 'filter', 'description': 'x', 'filter': {'field': 'split', 'operator': '=', 'value': 'a'}}]


class _Client:
    def __init__(self):
        self.sent = []

    def create_view(self, dataset_id, name, content, description=None, is_public=False, username=None):
        self.sent.append(is_public)
        return {'id': 1}


def dataset():
    ds = Dataset.__new__(Dataset)  # no network: create_view only calls the client
    ds._id = 1
    ds.client = _Client()
    return ds


def test_a_view_is_private_unless_made_public():
    ds = dataset()
    ds.create_view('mine', STAGES)
    ds.create_view('everyone', STAGES, is_public=True)
    assert ds.client.sent == [False, True]
