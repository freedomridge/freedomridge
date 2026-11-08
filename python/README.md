# Freedom Ridge Python SDK

The Python SDK for [Freedom Ridge](https://github.com/freedomridge/freedomridge) (FRIDGE), a self-hosted
tool for exploring and curating computer vision datasets. It adds samples, labels and embeddings to a
Freedom Ridge server, and filters, searches and edits them.

```bash
pip install freedomridge
```

Install the SDK of your server's major version: an SDK and a server of different major versions refuse
each other, and the message names the SDK to install. The base package needs only `requests`, `numpy`,
`Pillow` and `tqdm`. The compute helpers need extras: `embeddings`, `huggingface-embeddings`,
`patch-embeddings`, `metrics`, `hashes`, `quality`, or `all` (`pip install "freedomridge[all]"`).

Create a token in the app under **Settings → API Tokens**, then:

```python
import fridge
from fridge import Dataset, Sample

fridge.connect("https://fridge.example.com/api", api_token="...")  # or set FRIDGE_API_URL and FRIDGE_API_TOKEN

ds = Dataset("my_dataset")  # loads the dataset, or creates it (creating needs an admin token)
ds.add_samples(
    [
        # Local paths must be inside the server's FRIDGE_DATA_ROOT; s3:// URIs also work.
        Sample("/path/to/data/0001.jpg", tags=["train"], metadata={"category": "dog"}, location=(-122.4, 37.8)),
    ]
)

s = ds.samples
for sample in s[(s.category == "dog") & s.tags.contains("train")].limit(10):
    print(sample.filepath, sample.metadata)

ds.compute_embeddings()  # CLIP, computed on this machine; needs the `embeddings` extra
similar = s.all().sort_by_similarity(text="a dog on a beach")
```

The [SDK reference](https://github.com/freedomridge/freedomridge/blob/main/docs/SDK.md) covers the rest, and
the [changelog](https://github.com/freedomridge/freedomridge/blob/main/CHANGELOG.md) lists each release's
changes.
