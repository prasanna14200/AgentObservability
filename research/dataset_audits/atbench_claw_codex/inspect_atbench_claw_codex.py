from datasets import Dataset, DatasetDict, load_dataset


dataset_names = [
    "AI45Research/ATBench-Claw",
    "AI45Research/ATBench-Codex",
]


for name in dataset_names:
    print("\n" + "=" * 70)
    print(name)
    print("=" * 70)

    try:
        ds = load_dataset(name)

        print("Available configurations:")
        print(ds)

        if isinstance(ds, DatasetDict):
            for split_name, split in ds.items():
                print(f"\n--- Split: {split_name} ---")
                print("Rows:", len(split))
                print("Columns:", split.column_names)

                if len(split) > 0:
                    print("First example:")
                    print(split[0])
        elif isinstance(ds, Dataset):
            print("\n--- Dataset ---")
            print("Rows:", len(ds))
            print("Columns:", ds.column_names)

            if len(ds) > 0:
                print("First example:")
                print(ds[0])
        else:
            print("Unhandled dataset object type:", type(ds).__name__)

    except Exception as e:
        print("ERROR:")
        print(type(e).__name__)
        print(e)

