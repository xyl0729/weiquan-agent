def search_hybrid(*args: object, **kwargs: object) -> None:
    raise NotImplementedError(
        "v1 仅实现本地 FTS5 检索；达到召回测试门禁后再评估混合检索"
    )

