# NeMo Guardrails: The Missing Manual

> In this guide we explain all there is to know about Nvidia's NeMo Guardrails library for building better conversational AI. Learn how to apply guardrails for more controllable, user-friendly chatbots.

James Briggs · 2023-08-11

The adoption of chatbots across industries is unparalleled by any previous technology event. Gartner predicts that by 2027, chatbots will become the primary communication channel for ~25% of _all_ organizations [1].

We would use these two configuration files to initialize a `LLMRails` object like so:

```
from nemoguardrails import LLMRails, RailsConfig

# initialize rails config
config = RailsConfig.from_content(
    yaml_content=yaml_content
    colang_content=colang_content
)
# create rails
rails = LLMRails(config)
```

With our rails initialized, we can begin asking questions and interacting with our Guardrails protected LLM.

```json
{
  "_key": "ddfc11e011f7",
  "_type": "colabBlock",
  "jsonContent": "{\n  \"cells\": [\n    {\n      \"cell_type\": \"code\",\n      \"execution_count\": 5,\n      \"metadata\": {\n        \"colab\": {\n          \"base_uri\": \"https://localhost:8080/\"\n        },\n        \"id\": \"htzi4K72jUJq\",\n        \"outputId\": \"8615c158-9d06-4423-b454-d7489ddb9e30\"\n      },\n      \"outputs\": [\n        {\n          \"output_type\": \"stream\",\n          \"name\": \"stdout\",\n          \"text\": [\n            \"Hi there! How can I help you?\\n\",\n            \"How are you doing today?\\n\"\n          ]\n        }\n      ],\n      \"source\": [\n        \"res = await rails.generate_async(prompt=\\\"Hey there!\\\")\\n\",\n        \"print(res)\"\n      ]\n    }\n  ],\n  \"metadata\": {\n    \"language_info\": {\n      \"name\": \"python\"\n    },\n    \"orig_nbformat\": 4,\n    \"colab\": {\n      \"provenance\": []\n    },\n    \"kernelspec\": {\n      \"name\": \"python3\",\n      \"display_name\": \"Python 3\"\n    }\n  },\n  \"nbformat\": 4,\n  \"nbformat_minor\": 0\n}"
}
```

With this typical greeting, we don't activate any protective guardrails. However, we see the `greeting` flow is activated as the chatbot generates one line from the `bot express greeting` message and then follows with a new line generated from the `bot ask how are you` message.

Let's try asking a more political question:

```json
{
  "_key": "ed15e1574712",
  "_type": "colabBlock",
  "jsonContent": "{\n  \"cells\": [\n    {\n      \"cell_type\": \"code\",\n      \"execution_count\": 6,\n      \"metadata\": {\n        \"colab\": {\n          \"base_uri\": \"https://localhost:8080/\"\n        },\n        \"id\": \"n-e24M6PjUJq\",\n        \"outputId\": \"e8f5c8b4-4a1a-495f-f6a9-4d86864933c4\"\n      },\n      \"outputs\": [\n        {\n          \"output_type\": \"stream\",\n          \"name\": \"stdout\",\n          \"text\": [\n            \"I'm a shopping assistant, I don't like to talk of politics.\\n\",\n            \"However, I can help you with shopping related tasks. Is there anything I can help you with?\\n\"\n          ]\n        }\n      ],\n      \"source\": [\n        \"res = await rails.generate_async(prompt=\\\"what do you think of the president?\\\")\\n\",\n        \"print(res)\"\n      ]\n    }\n  ],\n  \"metadata\": {\n    \"language_info\": {\n      \"name\": \"python\"\n    },\n    \"orig_nbformat\": 4,\n    \"colab\": {\n      \"provenance\": []\n    },\n    \"kernelspec\": {\n      \"name\": \"python3\",\n      \"display_name\": \"Python 3\"\n    }\n  },\n  \"nbformat\": 4,\n  \"nbformat_minor\": 0\n}"
}
```

Here we see the `bot answer politics` message block is activated, returning our hardcoded response of `"I'm a shopping assistant, I don't like to talk of politics"`. Following this, the chatbot generates a response from the `bot offer help` message.

Using very simple guardrails, we've successfully managed to protect our chatbot against the topic of politics. This example is one use case of guardrails — but there are many more. Let's take a look at a few of those.
