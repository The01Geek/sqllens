`llm.max_tokens` (`SQLLENS_LLM__MAX_TOKENS`, default `8192`, range 1–128000):
output-token cap for every agent LLM call, wired into `AgentConfig.max_tokens`
by `build_agent`. Previously unset, so each call fell back to 512 tokens. (#247)
