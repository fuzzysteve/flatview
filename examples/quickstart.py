"""The README quick start, runnable: uv run python examples/quickstart.py"""

import os
import tempfile

import flatview

HERE = os.path.dirname(os.path.abspath(__file__))

schema = flatview.load_schema(os.path.join(HERE, "shop.fbs"))     # or a .bfbs from flatc

# Write: plain Python data in, FlatBuffer bytes out
data = schema.encode({"products": [
    {"sku": "T-100", "name": "Hammer", "price": 12.5, "stock": 40, "category": "Tool",
     "size": {"w": 30.0, "h": 12.0, "d": 3.0}, "tags": ["steel", "claw"]},
    {"sku": "F-007", "name": "Tea", "stock": 0, "category": "Food"},
    {"sku": "K-042", "name": "Kite", "price": 20.0, "category": "Toy"},
]})

with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "catalog.shop")
    with open(path, "wb") as fh:
        fh.write(data)

    # Read: memory-mapped, nothing is decoded until you touch it
    with flatview.open(path, schema) as fb:
        products = fb.root.products
        print(len(products))                         # 3
        print(products.keys())                       # ['F-007', 'K-042', 'T-100']  sorted by key on write
        hammer = products.lookup("T-100")            # binary search on the `key` field
        print(hammer.name, hammer.price)             # Hammer 12.5
        print(hammer.category.name)                  # Tool  (an IntEnum member)
        w, h, d = hammer.size                        # structs unpack like tuples
        print(w, h, d)                               # 30.0 12.0 3.0
        print(list(hammer.tags))                     # ['steel', 'claw']
        tea = products.lookup("F-007")
        print(tea.price, tea._has("price"))          # None False  (optional scalar)
        print(products.find("X-999"))                # None
        print(tea._asdict(defaults=False, enums="name"))
        # {'sku': 'F-007', 'name': 'Tea', 'category': 'Food'}
        fb.verify()                                  # full structural check; raises on corruption
