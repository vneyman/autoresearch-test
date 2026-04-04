# Experiment: Optimize Prompt

## Objective
Optimize the `prompt` for ETF research to maximize:
1. **Complete Results** — list all available ETFs available for an investor in the United States
2. **Recommendation** — recommend top 5 ETF and reason why it is selected
3. **Member Allocation** — ETFs recommended should include and allocation to `stocks`
4. **Presentation** - table format with ETF ticker, description, percentage allocation to the `stocks`, ETF expenses. Sorted by highest allocation.

## Constraints
- Prompt must be under 200 words
- `stocks` is defined as GOOGLE, Alphabet and any other reference to Alphabet stock

## Strategy
- Start with basic prompt like "top 10 ETFs that holds Google stock"
- For each: read current prompt → rewrite → evaluate

## Evaluation
Use llm_judge_prompt evaluator.
Metric: quality_score (0-100)
Direction: higher is better
