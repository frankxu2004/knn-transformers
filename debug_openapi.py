import os
import openai
import time

openai.api_key = os.getenv("OPENAI_API_KEY")

openai.api_key = "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"

for i in range(100):
    response = openai.Completion.create(
        model="code-davinci-002",
        prompt="I love how it looks like: " + str(i) + '\nHello? ' * 1,
        temperature=0,
        max_tokens=32,
        top_p=1,
    )
    print("Success.")
    print(i)
    print(response['usage'])
    time.sleep(10)
    