`llm.temperature` (`SQLLENS_LLM__TEMPERATURE`, 0–2) and `llm.effort`
(`SQLLENS_LLM__EFFORT`, `low|medium|high|xhigh|max`). Both are unset by
default, and SQL Lens sends nothing for a setting that is unset. A set
effort is sent as Anthropic `output_config.effort`. SQL Lens no longer
sends a hard-coded temperature of 0.7, which models that reject any
temperature answered with a 400. (#262)
