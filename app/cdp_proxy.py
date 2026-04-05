from __future__ import annotations

import argparse
import socket
import threading


def _forward(source: socket.socket, target: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(65536)
            if not chunk:
                break
            target.sendall(chunk)
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _rewrite_http_host_header(payload: bytes, *, target_host: str, target_port: int) -> bytes:
    if b"\r\n\r\n" not in payload:
        return payload
    header_block, remainder = payload.split(b"\r\n\r\n", 1)
    if not header_block.startswith((b"GET ", b"POST ", b"PUT ", b"DELETE ", b"OPTIONS ", b"HEAD ", b"CONNECT ")):
        return payload

    replacement = f"Host: {target_host}:{target_port}".encode("ascii")
    origin_replacement = f"Origin: http://{target_host}:{target_port}".encode("ascii")
    lines = header_block.split(b"\r\n")
    rewritten_lines: list[bytes] = []
    replaced = False
    for line in lines:
        if not replaced and line.lower().startswith(b"host:"):
            rewritten_lines.append(replacement)
            replaced = True
        elif line.lower().startswith(b"origin:"):
            rewritten_lines.append(origin_replacement)
        else:
            rewritten_lines.append(line)
    rewritten = b"\r\n".join(rewritten_lines)
    return rewritten + b"\r\n\r\n" + remainder


def _read_client_preface(client: socket.socket) -> bytes:
    client.settimeout(2)
    chunks: list[bytes] = []
    total = 0
    try:
        while total < 65536:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\r\n\r\n" in chunk or b"\r\n\r\n" in b"".join(chunks):
                break
    except TimeoutError:
        pass
    finally:
        client.settimeout(None)
    return b"".join(chunks)


def _extract_http_request_metadata(payload: bytes) -> tuple[str | None, str | None]:
    if b"\r\n\r\n" not in payload:
        return None, None
    header_block = payload.split(b"\r\n\r\n", 1)[0]
    lines = header_block.split(b"\r\n")
    if not lines or b" " not in lines[0]:
        return None, None
    try:
        _method, path, _version = lines[0].decode("ascii").split(" ", 2)
    except ValueError:
        return None, None
    host_header = None
    for line in lines[1:]:
        if line.lower().startswith(b"host:"):
            host_header = line.split(b":", 1)[1].strip().decode("ascii", errors="ignore")
            break
    return path, host_header


def _read_http_response(upstream: socket.socket) -> bytes:
    upstream.settimeout(5)
    buffer = bytearray()
    try:
        while b"\r\n\r\n" not in buffer:
            chunk = upstream.recv(4096)
            if not chunk:
                return bytes(buffer)
            buffer.extend(chunk)

        header_block, remainder = bytes(buffer).split(b"\r\n\r\n", 1)
        content_length = None
        for line in header_block.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
                break
        if content_length is None:
            while True:
                chunk = upstream.recv(4096)
                if not chunk:
                    break
                buffer.extend(chunk)
            return bytes(buffer)

        while len(remainder) < content_length:
            chunk = upstream.recv(4096)
            if not chunk:
                break
            buffer.extend(chunk)
            remainder = bytes(buffer).split(b"\r\n\r\n", 1)[1]
        return bytes(buffer)
    finally:
        upstream.settimeout(None)


def _rewrite_json_discovery_response(
    payload: bytes,
    *,
    public_host: str,
    public_port: int,
    target_host: str,
    target_port: int,
) -> bytes:
    if b"\r\n\r\n" not in payload:
        return payload
    header_block, body = payload.split(b"\r\n\r\n", 1)
    rewritten_body = body.replace(
        f"ws://{target_host}:{target_port}/".encode("ascii"),
        f"ws://{public_host}:{public_port}/".encode("ascii"),
    )
    rewritten_lines: list[bytes] = []
    replaced_length = False
    for line in header_block.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            rewritten_lines.append(f"Content-Length: {len(rewritten_body)}".encode("ascii"))
            replaced_length = True
        else:
            rewritten_lines.append(line)
    if not replaced_length:
        rewritten_lines.append(f"Content-Length: {len(rewritten_body)}".encode("ascii"))
    return b"\r\n".join(rewritten_lines) + b"\r\n\r\n" + rewritten_body


def _handle_client(client: socket.socket, target_host: str, target_port: int, listen_port: int) -> None:
    upstream = socket.create_connection((target_host, target_port))
    try:
        preface = _read_client_preface(client)
        path, public_host_header = _extract_http_request_metadata(preface)
        if preface:
            upstream.sendall(_rewrite_http_host_header(preface, target_host=target_host, target_port=target_port))
        if path and path.startswith("/json"):
            public_host = (public_host_header or "").split(":", 1)[0] or "127.0.0.1"
            response = _read_http_response(upstream)
            client.sendall(
                _rewrite_json_discovery_response(
                    response,
                    public_host=public_host,
                    public_port=listen_port,
                    target_host=target_host,
                    target_port=target_port,
                )
            )
            return
        threads = [
            threading.Thread(target=_forward, args=(client, upstream), daemon=True),
            threading.Thread(target=_forward, args=(upstream, client), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        client.close()
        upstream.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward a local CDP socket so it is reachable from sibling containers.")
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, required=True)
    args = parser.parse_args()

    server = socket.create_server((args.listen_host, args.listen_port), reuse_port=False)
    try:
        while True:
            client, _ = server.accept()
            threading.Thread(
                target=_handle_client,
                args=(client, args.target_host, args.target_port, args.listen_port),
                daemon=True,
            ).start()
    finally:
        server.close()


if __name__ == "__main__":
    main()
