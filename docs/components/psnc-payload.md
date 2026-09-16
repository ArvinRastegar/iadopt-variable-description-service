# `psnc-payload`

## 1. Responsibility

Build the JSON body for a PSNC chat-completions request in the measured
configuration — exactly the body that was measured, with no extra keys.

Module: `backend/app/clients/psnc_client.py` (existing). This work **adds** one
function and leaves `build_psnc_chat_payload` completely untouched, so the legacy
path cannot regress. No flag threading, no shared branches.

It does not send the request, retry, or parse responses — those stay in
`services/llm.py`.

## 2. The divergence being corrected

The measured request body is:

```json
{"model": "Qwen3.8-27B", "messages": [{"role": "user", "content": "..."}],
 "stream": false, "temperature": 0.5, "top_p": 1.0, "max_tokens": 16000,
 "chat_template_kwargs": {"enable_thinking": false}}
```

`build_psnc_chat_payload` today sends **both** reasoning switches — a top-level
`enable_thinking: false` *and* `chat_template_kwargs` — and sends neither `top_p`
nor `max_tokens`. The top-level key was added for Qwen3.5 compatibility across
providers and is deliberately kept for the legacy path; it was not present in any
measured call, so it has no place in the measured one.

## 3. Inputs

- `model: str` — resolved PSNC model name, from `resolve_model_name`.
- `prompt: str` — the rendered measured prompt, from `measured-prompt`.
- `temperature: float`, `top_p: float`, `max_tokens: int` — from `settings`.
- `stream: bool` — from the caller (`/decompose` false, `/decompose/stream` true).

## 4. Outputs

A `dict` with **exactly seven keys**: `model`, `messages`, `stream`, `temperature`,
`top_p`, `max_tokens`, `chat_template_kwargs`. No others, ever.

`messages` is a single-element list holding one `{"role": "user", "content": prompt}`.
There is no system message — the measured configuration has none, and adding one
would change the request.

## 5. Processing and invariants

- The key set is **exactly** the seven above. A test asserts set equality, not
  subset containment, because the failure this guards against is an extra key
  appearing, not a missing one.
- No top-level `enable_thinking`.
- `chat_template_kwargs` is exactly `{"enable_thinking": False}`.
- Exactly one message, role `user`, content the prompt verbatim.
- `stream` is the only field the caller varies between the two endpoints.
- Fully deterministic.

## 6. State and side effects

None. Pure function. No network, no globals, no logging. Headers and URL come from
the existing `psnc_chat_headers()` and `psnc_chat_completions_url()`, unchanged —
the latter already resolves to `https://llm.hpc.psnc.pl/v1/chat/completions`, which
matches the lab's `default_base_url` + `chat_completions_path`.

## 7. Secrets

None handled here. The bearer token is added by `psnc_chat_headers()`, which raises
`RuntimeError` when `PSNC_API_KEY` is unset. The payload must never carry a
credential, and the prompt must never be logged alongside one.

## 8. Public interface

### `build_measured_psnc_payload(model: str, prompt: str, *, temperature: float, top_p: float, max_tokens: int, stream: bool = False) -> Dict[str, Any]`

- **Input:** `model` and `prompt` non-empty; the three sampling values as given —
  this function does not read `settings` itself, so tests can drive it directly.
- **Action:** construct the seven-key dict.
- **Output:** as section 4.
- **Raises:** nothing. Invalid values are the caller's problem; this is a builder.
- **Side effects:** none.
- **Determinism:** fully deterministic.

## 9. Acceptance tests

1. The returned key set is **exactly**
   `{model, messages, stream, temperature, top_p, max_tokens, chat_template_kwargs}`.
2. `"enable_thinking" not in payload` — the top-level switch is absent.
3. `payload["chat_template_kwargs"] == {"enable_thinking": False}`.
4. `messages == [{"role": "user", "content": prompt}]` — one message, no system role.
5. With the measured settings, `temperature == 0.5`, `top_p == 1.0`,
   `max_tokens == 16000`.
6. `stream=False` by default; `stream=True` sets only that key and leaves the other
   six identical.
7. The payload serializes with `json.dumps` without error.
8. **Regression:** `build_psnc_chat_payload` output is unchanged for the same
   arguments — still carries both reasoning switches, still omits `top_p` and
   `max_tokens`. The legacy path was not touched.
9. `psnc_chat_completions_url()` ends in `/v1/chat/completions`.
