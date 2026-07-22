#!/usr/bin/env python
from __future__ import annotations

import os
from typing import Any

from elasticsearch import Elasticsearch


def create_client(
    *,
    url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    ca_certs: str | None = None,
    verify_certs: bool | None = None,
) -> Elasticsearch:
    """Create an Elasticsearch client without doing network I/O at import time."""

    target_url = url or os.getenv("DEEPEM_ES_URL", "https://localhost:9200")
    target_username = username if username is not None else os.getenv("DEEPEM_ES_USERNAME", "elastic")
    target_password = password if password is not None else os.getenv("DEEPEM_ES_PASSWORD", "work4deepem")
    target_ca_certs = ca_certs if ca_certs is not None else os.getenv("DEEPEM_ES_CA_CERTS")

    kwargs: dict[str, Any] = {}
    if target_username:
        kwargs["basic_auth"] = (target_username, target_password or "")
    if target_ca_certs:
        kwargs["ca_certs"] = target_ca_certs
    if verify_certs is not None:
        kwargs["verify_certs"] = verify_certs

    return Elasticsearch(target_url, **kwargs)


def smoke_test(index_name: str = "test_docs") -> None:
    """Optional manual smoke test: create/search a tiny document."""

    es = create_client()
    print(es.info())

    if not es.indices.exists(index=index_name):
        es.indices.create(
            index=index_name,
            mappings={
                "properties": {
                    "title": {"type": "text"},
                    "content": {"type": "text"},
                }
            },
        )

    es.index(
        index=index_name,
        id="1",
        document={
            "title": "hello elasticsearch",
            "content": "this is my first document indexed from python",
        },
    )
    es.indices.refresh(index=index_name)

    resp = es.search(index=index_name, query={"match": {"content": "first document"}})
    for hit in resp["hits"]["hits"]:
        print(hit["_score"], hit["_source"])


if __name__ == "__main__":
    smoke_test()
