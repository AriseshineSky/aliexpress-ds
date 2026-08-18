from aliexpress_ds.queue import ProductQueue


class _Pipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def sadd(self, key, value):
        self.ops.append(("sadd", key, value))
        return self

    def sismember(self, key, value):
        self.ops.append(("sismember", key, value))
        return self

    def lpush(self, key, value):
        self.ops.append(("lpush", key, value))
        return self

    def rpush(self, key, value):
        self.ops.append(("rpush", key, value))
        return self

    def execute(self):
        results = []
        for command, key, value in self.ops:
            if command == "sadd":
                values = self.client.sets.setdefault(key, set())
                added = value not in values
                values.add(value)
                results.append(1 if added else 0)
            elif command == "sismember":
                results.append(1 if value in self.client.sets.get(key, set()) else 0)
            elif command == "lpush":
                self.client.lists.setdefault(key, []).insert(0, value)
                results.append(1)
            else:
                self.client.lists.setdefault(key, []).append(value)
                results.append(1)
        return results


class _Redis:
    def __init__(self):
        self.sets = {}
        self.lists = {}

    def pipeline(self, transaction=False):
        assert transaction is False
        return _Pipeline(self)


def test_enqueue_many_counts_new_items_when_seen_dedup_is_enabled() -> None:
    queue = object.__new__(ProductQueue)
    queue.client = _Redis()
    queue.queue_key = "queue"
    queue.seen_key = "seen"
    queue.failed_key = "failed"

    new, skipped = queue.enqueue_many(
        [
            {"product_id": "1"},
            {"product_id": "1"},
            {"product_id": "2"},
        ],
        use_seen=True,
    )

    assert (new, skipped) == (2, 1)
    assert len(queue.client.lists["queue"]) == 2


def test_enqueue_many_skips_permanent_failures() -> None:
    queue = object.__new__(ProductQueue)
    queue.client = _Redis()
    queue.queue_key = "queue"
    queue.seen_key = "seen"
    queue.failed_key = "failed"
    queue.client.sets["failed"] = {"bad"}

    new, skipped = queue.enqueue_many(
        [
            {"product_id": "bad"},
            {"product_id": "ok"},
        ],
        use_seen=True,
    )

    assert (new, skipped) == (1, 1)
    payloads = queue.client.lists["queue"]
    assert len(payloads) == 1
    assert "ok" in payloads[0]
