import google.generativeai as genai

genai.configure(api_key="AIzaSyBnh-2V18d36gjjbmirfRlP5-_HcoR0Clc")

model = genai.GenerativeModel("gemini-2.5-flash")

response = model.generate_content("Say hello")

print(response.text)