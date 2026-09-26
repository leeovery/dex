> ## Documentation Index
> Fetch the complete documentation index at: https://docs.typesafe.ai/llms.txt
> Use this file to discover all available pages before exploring further.

# Jev 1.13 jaggedness

> Jev isn't perfect. Here are some jagged edges we are aware of with jev-1.13. Many of these will be fixed in later versions.

<Note>
  **Applies to `jev-1.13`.** Last reviewed 2026-09-17.
</Note>

`jev-1.13` is fast, calibrated, and good at common-sense judgment but it is not perfect. `jev-1.13` does the best on [System One](/concepts/system-one) tasks. It may struggle with tasks that require additional levels of indirection. It can be quite literal in its understanding. It struggles with tasks that require numeric precision.

## The failure modes in detail

| # | Failure mode                                                                        | Do this instead                                                |
| - | ----------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| 1 | [Literal reading](#literal-reading)                                                 | Write the exact condition, criteria for each available options |
| 2 | [Math and Numbers](#math-and-numbers)                                               | Keep the arithmetic in code                                    |
| 3 | [Date and time comparison](#date-and-time-comparison)                               | Extract components; compare in code                            |
| 4 | [Indirection](#indirection)                                                         | Reduce hops; point to the relevant state                       |
| 5 | [Large state full of irrelevant detail](#large-state-full-of-irrelevant-detail)     | Filter first; send only what the question needs                |
| 6 | [Adversarial content](#adversarial-content)                                         | Write precise prompts, and test edge cases before deploying    |
| 7 | [Contradictory instructions and criteria](#contradictory-instructions-and-criteria) | Align the criteria and instruction                             |
| 8 | [Common-sense structural invariants](#common-sense-structural-invariants)           | Ask each decision one way; enforce identities in code          |
| 9 | [Generation](#generation)                                                           | Use a generative model                                         |

## Literal reading

`jev-1.13` answers the question you wrote, not the one you meant. Scoping words, negations, and implied conditions are read at face value. A question will be answered based on the words written in the instruction, whereas a person might have read the intent behind the instructions.

**Instead:** state the exact condition in the `instructions`. Be specific. Put boundary cases in the criteria. When you look at a wrong answer and find yourself explaining what you really meant, that explanation is the missing half of the instruction. Where interpretation is unavoidable, split it into two literal questions and combine them in code.

## Math and Numbers

Jev is not a calculator. We strongly recommend implementing any mathematical logic in code. Jev will perform better on semantic questions than mathematical ones.

### Counting

`jev-1.13` does not count reliably. This covers characters in a word, occurrences of a term in a passage, and items in a long list. The model recognizes the shape of an answer rather than tallying, and the error grows with the size of the thing being counted.

Before asking a counting question, ask why the count needs a model at all. If the unit is something a regular expression or a parser can find, the count belongs in code and the model has nothing to add.

**Instead:** count in code. When you want to count items matching some criteria, iterate in code over the candidates and ask one question for each, then add up the answers yourself.

```python theme={null}
from typesafe_sdk import Noul, TypeSafeClient

client = TypeSafeClient(model="jev-1.13")
YES = 0.5  # up to you on what you want the threshold to be, depends on your usecase.

items = ["typesafe", "apple", "california", "banana", "likes", "calibration", "orange", "vertex"]

result = client.system_one(
    {"items": items},
    {
        f"item_{i}": Noul(instructions=f"Is `items[{i}]` the name of a fruit?")
        for i in range(len(items))
    },
)

count = sum(result.nouls[f"item_{i}"].noul > YES for i in range(len(items)))
```
