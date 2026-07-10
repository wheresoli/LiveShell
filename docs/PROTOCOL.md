# LiveShell Protocol

LiveShell speaks newline-delimited JSON over a local transport. Each request and response is one JSON object plus `\n`. The same request/response protocol is served over two interchangeable transports (see [Transports](#transports)): the stdio pipes of a launched daemon, or a loopback TCP socket.

Current protocol version: `1.0`

## Envelope

Request:

```json
{"id":"req_1","method":"capability.discover","params":{}}
```

Success response:

```json
{"id":"req_1","ok":true,"result":{}}
```

Error response:

```json
{"id":"req_1","ok":false,"error":{"type":"ValueError","code":"invalid_params","message":"..."}}
```

Stable error codes:

- `invalid_request`
- `invalid_params`
- `not_found`
- `conflict`
- `unknown_method`
- `internal_error`

## Methods

- `capability.discover`
- `daemon.status`
- `daemon.shutdown`
- `session.create`
- `session.list`
- `session.snapshot`
- `session.close`
- `command.start`
- `command.poll`
- `command.events`
- `command.cancel`
- `command.result`

`capability.discover` returns `protocol_version` and a capability list.

`command.start` validates params before creating a command record. Commands for the same session execute FIFO: new commands are `queued` until the session worker starts them, then move to `starting` and `running`.

`command.events` returns events with stable per-command sequence numbers greater than `since_seq`.

`daemon.shutdown` is reliable over a live channel (stdio or socket). The `liveshell daemon shutdown`/`stop` CLI paths also write a state-dir marker for operators when no live channel is held; the marker is not a network control plane.

## Transports

The protocol is transport-agnostic. Two transports are supported, both local-only:

- **stdio** — `liveshell daemon stdio` serves the protocol over the process's stdin/stdout. The daemon's lifetime is bound to the launching process's pipes. This is the default, used by `LiveShellClient.stdio(...)` and `liveshell run`.
- **socket** — `liveshell daemon serve`/`daemon start` serves the protocol over a **loopback IPv4 TCP socket** (default `127.0.0.1`, an ephemeral port unless one is given). The bound address is published to `daemon.json` in the state dir so a fresh client can attach with `LiveShellClient.connect(state_dir)`. Unlike stdio, a socket daemon keeps running — and its commands keep executing — after any client disconnects.
The host is validated as loopback-only: non-loopback and non-IPv4 hosts are rejected. There is no remote transport and no auto-started network listener; the socket daemon is opt-in.

## Security

The protocol is local-only. LiveShell does not install URL handlers, expose a *remote* network server, or execute commands from links. The optional socket transport binds to loopback (`127.0.0.1`) only and is never started implicitly.
