# Compatible version using older stable SDK
import google.generativeai as genai


def generate_gemini(messages, api_key):

    genai.configure(api_key=api_key)

    model = genai.GenerativeModel("gemini-pro")

    prompt = "\n".join([m["content"] for m in messages])

    response = model.generate_content(prompt)

    return response.text