from unittest.mock import patch


async def test_vault_index_wrapper_never_raises():
    import backend.scheduler as sched

    with patch("backend.graph.vault_index.refresh", side_effect=RuntimeError("boom")), \
         patch.object(sched.logger, "warning") as warn:
        await sched._graph_vault_index()
    warn.assert_called_once()


async def test_vault_index_wrapper_logs_result():
    import backend.scheduler as sched

    with patch("backend.graph.vault_index.refresh", return_value={"scanned": 3}), \
         patch.object(sched.logger, "info") as info:
        await sched._graph_vault_index()
    assert any("scanned" in str(c.args[0]) for c in info.call_args_list)
