# LiveShell Protocol

LiveShell speaks newline-delimited JSON over local stdio. Each request and response is one JSON object plus `\n`.

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

`daemon.shutdown` is reliable over the live stdio channel. CLI shutdown writes a state-dir marker for local operators; it is not a network control plane.

## Security

The protocol is local-only stdio in this slice. LiveShell does not install URL handlers, expose a default network server, or execute commands from links.
