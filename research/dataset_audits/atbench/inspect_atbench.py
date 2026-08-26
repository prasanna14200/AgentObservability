from datasets import load_dataset


atbench = load_dataset(
    "AI45Research/ATBench",
    "ATBench",
    split="test",
)

atbench500 = load_dataset(
    "AI45Research/ATBench",
    "ATBench500",
    split="test",
)


for name, ds in [
    ("ATBench (current)", atbench),
    ("ATBench500 (legacy)", atbench500),
]:
    print(f"\n--- {name} ---")
    print("Rows:", len(ds))
    print("Columns:", ds.column_names)
    print("First example:")
    print(ds[0])

