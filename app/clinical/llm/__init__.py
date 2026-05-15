"""
Anthropic Claude integration.

Modules (populated per documents/phase_3_implementation_plan.md M6/M7):

  * ``claude_client``      -- ClaudeClient wrapper around ``anthropic.Anthropic``
                              with retries, a circuit breaker, response-header
                              capture, and one ``LlmInvocation`` row per call (M6).
  * ``prompts``            -- shared XML-tagged prompt fragments used by the
                              ASAM (M8) and TJC (M9) narration prompts.
  * ``structured_output``  -- native Anthropic Structured Outputs with a
                              tool-use fallback (M7).
  * ``citation_validator`` -- post-hoc validation that every returned
                              ``citations[]`` round-trips against the local
                              DB (``raw_text[start:end] == cited_text``) (M7).

Design intent (phase_3_PRD.md §5.6): every Claude call goes through
``ClaudeClient`` -- no module talks to the SDK directly. That gives one
chokepoint for retries, throttling, cost logging, and tests-via-mocking.
"""
