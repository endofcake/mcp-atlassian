"""Unit tests for transport selection and execution."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_atlassian import _run_stdio_with_stdin_guard, main


class TestMainTransportSelection:
    """Test the main function's transport-specific execution logic."""

    @pytest.fixture
    def mock_server(self):
        """Create a mock server instance."""
        server = MagicMock()
        server.run_async = AsyncMock(return_value=None)
        return server

    @pytest.fixture
    def mock_asyncio_run(self):
        """Mock asyncio.run to capture what coroutine is executed."""
        with patch("asyncio.run") as mock_run:
            captured_coros = []

            def _capture(coro):
                captured_coros.append(coro)
                mock_run._called_with = coro

            mock_run.side_effect = _capture
            yield mock_run

            # Close all captured coroutines to avoid RuntimeWarning
            for coro in captured_coros:
                if hasattr(coro, "close"):
                    coro.close()

    @pytest.mark.parametrize("transport", ["sse", "streamable-http"])
    def test_http_transports_use_direct_execution(
        self, mock_server, mock_asyncio_run, transport
    ):
        """Verify HTTP transports use direct execution without stdin monitoring.

        This is a regression test for issues #519 and #524.
        """
        with patch("mcp_atlassian.servers.main.AtlassianMCP", return_value=mock_server):
            with patch.dict("os.environ", {"TRANSPORT": transport}):
                with patch("sys.argv", ["mcp-atlassian"]):
                    try:
                        main()
                    except SystemExit:
                        pass

                    # Verify asyncio.run was called
                    assert mock_asyncio_run.called

                    # Get the coroutine info
                    called_coro = mock_asyncio_run._called_with
                    coro_repr = repr(called_coro)

                    assert "_run_stdio_with_stdin_guard" not in coro_repr
                    assert "run_async" in coro_repr or hasattr(called_coro, "cr_code")

    def test_stdio_transport_uses_stdin_guard(self, mock_server, mock_asyncio_run):
        with patch("mcp_atlassian.servers.main.AtlassianMCP", return_value=mock_server):
            with patch.dict("os.environ", {"TRANSPORT": "stdio"}):
                with patch("sys.argv", ["mcp-atlassian"]):
                    try:
                        main()
                    except SystemExit:
                        pass

                    assert mock_asyncio_run.called
                    called_coro = mock_asyncio_run._called_with
                    coro_repr = repr(called_coro)
                    assert "_run_stdio_with_stdin_guard" in coro_repr

    @pytest.mark.parametrize("stateless", ["False", "True"])
    def test_stateless_set(self, mock_asyncio_run, stateless):
        """Verify that stateless_http is passed to run_async via run_kwargs."""
        from mcp_atlassian.servers import main_mcp

        with patch.object(
            main_mcp, "run_async", new_callable=AsyncMock
        ) as mock_run_async:
            with patch.dict(
                "os.environ",
                {"STATELESS": stateless, "TRANSPORT": "streamable-http"},
            ):
                with patch("sys.argv", ["mcp-atlassian"]):
                    try:
                        main()
                    except SystemExit:
                        pass

                    # Verify run_async was called
                    assert mock_run_async.called

                    # Verify stateless_http was passed correctly
                    call_kwargs = mock_run_async.call_args[1]
                    desired = stateless.lower() == "true"
                    assert call_kwargs["stateless_http"] == desired

    @pytest.mark.parametrize("transport", ["stdio", "sse"])
    def test_stateless_rejects_non_streamable_http(self, mock_asyncio_run, transport):
        """Verify that --stateless flag errors when used with non-streamable-http transport."""
        with patch.dict("os.environ", {"STATELESS": "true", "TRANSPORT": transport}):
            with patch("sys.argv", ["mcp-atlassian"]):
                with pytest.raises(SystemExit) as exc_info:
                    main()

                # Should exit with code 1 (error)
                assert exc_info.value.code == 1

    def test_cli_overrides_env_transport(self, mock_server, mock_asyncio_run):
        """Test that CLI transport argument overrides environment variable."""
        with patch("mcp_atlassian.servers.main.AtlassianMCP", return_value=mock_server):
            with patch.dict("os.environ", {"TRANSPORT": "sse"}):
                # Simulate CLI args with --transport stdio
                with patch("sys.argv", ["mcp-atlassian", "--transport", "stdio"]):
                    try:
                        main()
                    except SystemExit:
                        pass

                    called_coro = mock_asyncio_run._called_with
                    coro_repr = repr(called_coro)
                    assert "_run_stdio_with_stdin_guard" in coro_repr

    @pytest.mark.asyncio
    async def test_stdio_guard_cancels_server_when_parent_exits(self):
        server_started = asyncio.Event()
        server_cancelled = asyncio.Event()

        async def fake_run_async(**kwargs):
            del kwargs
            server_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                server_cancelled.set()
                raise

        async def fake_watch_parent(_stop_event) -> None:
            await server_started.wait()

        with patch(
            "mcp_atlassian.servers.main_mcp.run_async", side_effect=fake_run_async
        ):
            with patch(
                "mcp_atlassian._watch_parent_exit", side_effect=fake_watch_parent
            ):
                await _run_stdio_with_stdin_guard({"transport": "stdio"})

        assert server_cancelled.is_set()

    def test_signal_handlers_always_setup(self, mock_server):
        """Test that signal handlers are set up regardless of transport."""
        with patch("mcp_atlassian.servers.main.AtlassianMCP", return_value=mock_server):
            with patch("asyncio.run") as mock_run:
                # Patch where it's imported in the main module
                with patch("mcp_atlassian.setup_signal_handlers") as mock_setup:
                    with patch.dict("os.environ", {"TRANSPORT": "stdio"}):
                        with patch("sys.argv", ["mcp-atlassian"]):
                            try:
                                main()
                            except SystemExit:
                                pass

                            # Signal handlers should always be set up
                            mock_setup.assert_called_once()

                # Close captured coroutines
                for call in mock_run.call_args_list:
                    coro = call[0][0] if call[0] else None
                    if coro and hasattr(coro, "close"):
                        coro.close()

    def test_error_handling_preserved(self, mock_server):
        """Test that error handling works correctly for all transports."""
        # Make the server's run_async raise an exception when awaited
        error = RuntimeError("Server error")

        async def failing_run_async(**kwargs):
            raise error

        mock_server.run_async = failing_run_async

        leaked_coros = []

        def _raise_and_capture(coro):
            leaked_coros.append(coro)
            raise error

        with patch("mcp_atlassian.servers.main.AtlassianMCP", return_value=mock_server):
            with patch("asyncio.run") as mock_run:
                # Simulate the exception propagating through asyncio.run
                mock_run.side_effect = _raise_and_capture

                with patch.dict("os.environ", {"TRANSPORT": "stdio"}):
                    with patch("sys.argv", ["mcp-atlassian"]):
                        # The main function logs the error and exits with code 1
                        with patch("sys.exit") as mock_exit:
                            main()
                            # Verify error was handled - sys.exit called with 1 for error
                            # and then with 0 in the finally block
                            assert mock_exit.call_count == 2
                            assert mock_exit.call_args_list[0][0][0] == 1  # Error exit
                            assert (
                                mock_exit.call_args_list[1][0][0] == 0
                            )  # Finally exit

        # Close leaked coroutines to avoid RuntimeWarning
        for coro in leaked_coros:
            if hasattr(coro, "close"):
                coro.close()


class TestServerTLSConfiguration:
    """The server's own HTTPS listener (--ssl-certfile / --ssl-keyfile)."""

    @staticmethod
    def _make_pair(tmp_path, prefix=""):
        """Create dummy cert/key files (contents irrelevant; never read)."""
        cert = tmp_path / f"{prefix}server-cert.pem"
        key = tmp_path / f"{prefix}server-key.pem"
        cert.write_text("cert")
        key.write_text("key")
        return cert, key

    @pytest.fixture
    def cert_key(self, tmp_path):
        return self._make_pair(tmp_path)

    @staticmethod
    def _run_main():
        try:
            main()
        except SystemExit:
            pass

    @pytest.mark.parametrize("transport", ["sse", "streamable-http"])
    def test_tls_env_forwards_uvicorn_config(self, cert_key, transport):
        """Both certs via env -> uvicorn_config reaches run_async, for both
        HTTP transports."""
        from mcp_atlassian.servers import main_mcp

        cert, key = cert_key
        with patch.object(
            main_mcp, "run_async", new_callable=AsyncMock
        ) as mock_run_async:
            with patch.dict(
                "os.environ",
                {
                    "TRANSPORT": transport,
                    "MCP_SSL_CERTFILE": str(cert),
                    "MCP_SSL_KEYFILE": str(key),
                },
            ):
                with patch("sys.argv", ["mcp-atlassian"]):
                    self._run_main()

        assert mock_run_async.called
        assert mock_run_async.call_args[1]["uvicorn_config"] == {
            "ssl_certfile": str(cert),
            "ssl_keyfile": str(key),
        }

    def test_tls_cli_overrides_env(self, tmp_path):
        """CLI --ssl-* wins over a valid env pair and forwards the CLI paths."""
        from mcp_atlassian.servers import main_mcp

        cli_cert, cli_key = self._make_pair(tmp_path, "cli-")
        env_cert, env_key = self._make_pair(tmp_path, "env-")
        with patch.object(
            main_mcp, "run_async", new_callable=AsyncMock
        ) as mock_run_async:
            with patch.dict(
                "os.environ",
                {
                    "TRANSPORT": "streamable-http",
                    "MCP_SSL_CERTFILE": str(env_cert),
                    "MCP_SSL_KEYFILE": str(env_key),
                },
            ):
                with patch(
                    "sys.argv",
                    [
                        "mcp-atlassian",
                        "--ssl-certfile",
                        str(cli_cert),
                        "--ssl-keyfile",
                        str(cli_key),
                    ],
                ):
                    self._run_main()

        assert mock_run_async.call_args[1]["uvicorn_config"] == {
            "ssl_certfile": str(cli_cert),
            "ssl_keyfile": str(cli_key),
        }

    def test_tls_cli_cert_with_env_key(self, tmp_path):
        """A cross-source pair (cert via CLI, key via env) resolves and
        forwards both — the two paths resolve independently."""
        from mcp_atlassian.servers import main_mcp

        cli_cert, _ = self._make_pair(tmp_path, "cli-")
        _, env_key = self._make_pair(tmp_path, "env-")
        with patch.object(
            main_mcp, "run_async", new_callable=AsyncMock
        ) as mock_run_async:
            with patch.dict(
                "os.environ",
                {
                    "TRANSPORT": "streamable-http",
                    "MCP_SSL_KEYFILE": str(env_key),
                },
            ):
                with patch(
                    "sys.argv",
                    ["mcp-atlassian", "--ssl-certfile", str(cli_cert)],
                ):
                    self._run_main()

        assert mock_run_async.call_args[1]["uvicorn_config"] == {
            "ssl_certfile": str(cli_cert),
            "ssl_keyfile": str(env_key),
        }

    @pytest.mark.parametrize("transport", ["sse", "streamable-http"])
    def test_no_tls_omits_uvicorn_config(self, transport):
        """Without TLS configured, run_kwargs carries no uvicorn_config."""
        from mcp_atlassian.servers import main_mcp

        with patch.object(
            main_mcp, "run_async", new_callable=AsyncMock
        ) as mock_run_async:
            with patch.dict("os.environ", {"TRANSPORT": transport}):
                with patch("sys.argv", ["mcp-atlassian"]):
                    self._run_main()

        assert mock_run_async.called
        assert "uvicorn_config" not in mock_run_async.call_args[1]

    @pytest.mark.parametrize("provided", ["cert", "key"])
    def test_half_configured_tls_aborts(self, cert_key, provided):
        """Setting only one of cert/key exits with code 1 via the
        both-or-neither guard — before the server starts, not via some later
        uvicorn failure."""
        from mcp_atlassian.servers import main_mcp

        cert, key = cert_key
        env = {"TRANSPORT": "streamable-http"}
        if provided == "cert":
            env["MCP_SSL_CERTFILE"] = str(cert)
        else:
            env["MCP_SSL_KEYFILE"] = str(key)

        mock_logger = MagicMock()
        with patch("mcp_atlassian.setup_logging", return_value=mock_logger):
            with patch.object(
                main_mcp, "run_async", new_callable=AsyncMock
            ) as mock_run_async:
                with patch.dict("os.environ", env):
                    with patch("sys.argv", ["mcp-atlassian"]):
                        with pytest.raises(SystemExit) as exc_info:
                            main()

        assert exc_info.value.code == 1
        assert not mock_run_async.called  # aborted before the server started
        errors = " ".join(
            str(call.args[0]) for call in mock_logger.error.call_args_list
        )
        assert "must be provided together" in errors

    @pytest.mark.parametrize("missing", ["cert", "key"])
    def test_env_path_not_found_aborts(self, tmp_path, missing):
        """A cert/key path from env that isn't a file exits with code 1 via the
        existence guard before the server starts (env paths bypass click's
        existence check)."""
        from mcp_atlassian.servers import main_mcp

        cert, key = self._make_pair(tmp_path)
        env = {"TRANSPORT": "streamable-http"}
        if missing == "cert":
            env["MCP_SSL_CERTFILE"] = str(tmp_path / "nope-cert.pem")
            env["MCP_SSL_KEYFILE"] = str(key)
        else:
            env["MCP_SSL_CERTFILE"] = str(cert)
            env["MCP_SSL_KEYFILE"] = str(tmp_path / "nope-key.pem")

        mock_logger = MagicMock()
        with patch("mcp_atlassian.setup_logging", return_value=mock_logger):
            with patch.object(
                main_mcp, "run_async", new_callable=AsyncMock
            ) as mock_run_async:
                with patch.dict("os.environ", env):
                    with patch("sys.argv", ["mcp-atlassian"]):
                        with pytest.raises(SystemExit) as exc_info:
                            main()

        assert exc_info.value.code == 1
        assert not mock_run_async.called
        errors = " ".join(
            str(call.args[0]) for call in mock_logger.error.call_args_list
        )
        assert "not found" in errors

    def test_tls_under_stdio_warns_and_skips_uvicorn_config(self, cert_key):
        """Under stdio the certs are ignored (no HTTP listener) but the
        operator is warned rather than silently served plaintext, and
        run_kwargs never carries uvicorn_config."""
        cert, key = cert_key
        mock_logger = MagicMock()
        captured_kwargs: dict = {}

        async def _capture_guard(run_kwargs):
            captured_kwargs.update(run_kwargs)

        with patch("mcp_atlassian.setup_logging", return_value=mock_logger):
            with patch(
                "mcp_atlassian._run_stdio_with_stdin_guard",
                side_effect=_capture_guard,
            ):
                with patch.dict(
                    "os.environ",
                    {
                        "TRANSPORT": "stdio",
                        "MCP_SSL_CERTFILE": str(cert),
                        "MCP_SSL_KEYFILE": str(key),
                    },
                ):
                    with patch("sys.argv", ["mcp-atlassian"]):
                        self._run_main()

        warnings = " ".join(
            str(call.args[0]) for call in mock_logger.warning.call_args_list
        )
        assert "no effect" in warnings  # the cert-configured-under-stdio warning
        assert "uvicorn_config" not in captured_kwargs
