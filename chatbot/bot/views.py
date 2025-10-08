import os
import json
import re
import requests
import difflib
import concurrent.futures
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from bot.models import Product
from django.conf import settings
import traceback
# import google.generativeai as genai

# # Configure Gemini API
# genai.configure(api_key="AIzaSyCGcXyySarXhMKTEY83jE3J1tJL6OhXboE")

# models = genai.list_models()
# for m in models:
#     # Optional: filter only those supporting generateContent
#     if "generateContent" in m.supported_generation_methods:
#         print(m.name)
        
# model = genai.GenerativeModel("gemini-1.5-flash")


# --- Caching Setup ---
PAGES_CACHE = None
BRANDS_CACHE = None
PRODUCTS_CACHE = None


def get_pages_content():
    """Load pages_content.txt only once (fast performance)"""
    global PAGES_CACHE
    if PAGES_CACHE:
        return PAGES_CACHE
    if os.path.exists("pages_content.txt"):
        with open("pages_content.txt", "r", encoding="utf-8") as f:
            PAGES_CACHE = f.read()[:2000]  # limit to 2000 chars for speed
    return PAGES_CACHE or ""


def get_brands():
    """Extract brand names from products only once"""
    global BRANDS_CACHE
    if BRANDS_CACHE:
        return BRANDS_CACHE
    BRANDS_CACHE = list(set([p.title.split()[0] for p in Product.objects.all()]))
    return BRANDS_CACHE


def get_all_products():
    """Cache all products in memory for fast filtering"""
    global PRODUCTS_CACHE
    if PRODUCTS_CACHE:
        return PRODUCTS_CACHE
    PRODUCTS_CACHE = list(Product.objects.all())
    return PRODUCTS_CACHE


# def detect_language(user_query):
#     """Detect simple language: Roman Urdu vs English"""
#     urdu_words = ["mujhe", "kaun", "kon", "kaha", "dikhao", "dikho",
#                   "range", "mahanga", "sasta", "pao", "dard", "kis", "kaisa"]
#     return "urdu" if any(word in user_query.lower() for word in urdu_words) else "english"

def detect_language(text):
    import re
    if re.search(r"[\u0600-\u06FF]", text):
        return "urdu"
    return "english"

def parse_price_range(user_query):
    """Extract numeric price range if mentioned"""
    numbers = re.findall(r'\d+', user_query)
    if len(numbers) >= 2:
        low, high = int(numbers[0]), int(numbers[1])
        return low, high
    return None, None


def find_products(user_query):
    """Find relevant products based on query and price filters"""
    all_products = get_all_products()
    results = []
    low, high = parse_price_range(user_query)

    for p in all_products:
        title_lower = p.title.lower()

        # Price filtering
        price_match = True
        if low and high:
            try:
                price_val = float(p.price)
                if not (low <= price_val <= high):
                    price_match = False
            except:
                pass

        # Relevance matching
        relevance = difflib.SequenceMatcher(None, user_query.lower(), title_lower).ratio()
        if relevance > 0.3 and price_match:
            results.append((relevance, p))

    results = sorted(results, key=lambda x: x[0], reverse=True)[:10]  # top 10 products
    return [p for _, p in results]


def query_gemini(user_query, website_content, products, brands):
    """Ask Gemini to answer user query based on cached website data"""
    try:
        #product_text = "\n".join([f"{p.title} - Rs. {p.price}" for p in products])
        product_text = "\n".join([f"{p.title} - Rs. {getattr(p, 'price', 'N/A')}" for p in products])
        
        # Detect language
        language = detect_language(user_query)

        prompt = f"""
        You are a friendly **sales agent** for Royal Trend.

        📝 RULES:
        - Always reply in same language as user query.
        - Always reply in same language as user query(English and Roman Urdu).
        - If language is "urdu", reply in Roman Urdu (English alphabets only).
        - If language is "English", reply in English.
        - Keep replies short (3-6 lines), friendly & casual like WhatsApp chat.
        - No long paragraphs or formal tone., not like a long email.
        - Do NOT always start with "Assalamu Alaikum", use it rarely or skip.
        - If user asks for products, show max 2-3 best suggestions only.
        - End with a small question or call-to-action: e.g. "Aapko size bataun?" or "Aur options chahiye?"
        - If price range is given, suggest only products from that range.
        - If user mentions foot pain, suggest comfortable/orthopedic shoes.
        - No long paragraphs, just short helpful sentences.
        - **Reply to the user as fast as possible, ideally within 3 seconds.**

        Website Content:
        {website_content}

        Available Brands:
        {brands}

        Sample Products:
        {product_text}

        User asked: {user_query}
        """

        # model = genai.GenerativeModel("gemini-1.5-flash")
        # response = model.generate_content(prompt)
        # return response.text.strip()

        GEMINI_API_KEY = getattr(settings, "GEMINI_API_KEY", None)
        if not GEMINI_API_KEY:
            return "⚠️ Gemini API key not configured."

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
        headers = {"Content-Type": "application/json"}
        payload = {"contents": [{"parts": [{"text": prompt}]}]}

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        else:
            return f"⚠️ Gemini error: {response.status_code}"

        
    except Exception as e:
        return f"⚠️ Error while generating response: {str(e)}"


def query_with_timeout(user_query, website_content, products, brands, timeout=4):
    """Run Gemini query but fallback if slow"""
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(query_gemini, user_query, website_content, products, brands)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return "⏳ Server busy hai, mai aapko best products recommend kar raha hoon..."


def smart_query_handler(user_query):
    """Main handler that decides how to respond"""
    website_content = get_pages_content()
    brands_list = ", ".join(get_brands())

    # 🔎 First check if price range mentioned
    low, high = parse_price_range(user_query)
    if low and high:
        products = Product.objects.filter(price__gte=low, price__lte=high)[:5]
        if products:
            # ✅ Directly return short formatted text (fast)
            product_texts = [f"{p.title} – Rs. {p.price}" for p in products]
            return "Yeh options available hain:\n" + "\n".join(product_texts)

    # 🔎 Otherwise use fuzzy product finder
    products = find_products(user_query)
    if not products:  
        products = Product.objects.all()[:20]  # fallback

    # 🔎 Ask Gemini but with timeout
    return query_with_timeout(user_query, website_content, products, brands_list, timeout=4)

# @csrf_exempt
# def dialogflow_webhook(request):
#     if request.method == "POST":
#         try:
#             body = json.loads(request.body.decode("utf-8"))
#             print("📩 Raw Request Body:", body)  # log raw request
#         except Exception as e:
#             print("❌ Error decoding request body:", str(e))
#             traceback.print_exc()
#             return JsonResponse({"fulfillmentText": "⚠️ Invalid request body."}, status=400)

#         user_query = body.get("queryResult", {}).get("queryText", "")
#         intent = body.get("queryResult", {}).get("intent", {}).get("displayName", "")
        
#         print(f"📝 Extracted User Query: {user_query}")
#         print(f"🎯 Detected Intent: {intent}")

#         answer = "Sorry, I couldn't generate a reply."

#         # --- Intent Handling ---

#         # ✅ LLM Query (always highest priority)
#         if intent == "LLMQueryIntent":
#             try:
#                 print(f"📝 User Query: {user_query}")
#                 answer = query_with_timeout(
#                     user_query,
#                     website_content=get_pages_content(),
#                     products=Product.objects.all()[:50],
#                     brands=", ".join(get_brands()),
#                     timeout=4
#                 )
#                 if not answer or "⏳" in answer:
#                     return JsonResponse({
#                         "fulfillmentText": "⏳ Thoda waqt lag raha hai, lekin mai aapko best shoes suggest karta hoon..."
#                     })
#                 if not answer or len(answer.strip()) < 5:
#                     answer = "Maaf kijiye! Aapke liye sahi jawab nahi mila, lekin mai aapko kuch best shoes suggest kar sakta hoon 👉 https://royaltrend.pk"
#             except Exception as e:
#                 print("❌ Error in LLMQueryIntent:", str(e))
#                 answer = "⚠️ Kuch problem hui, lekin aap hamari website https://royaltrend.pk par check kar sakte ho."


#         # ✅ Default Fallback Intent (runs when none match)
#         else:
#             answer = smart_query_handler(user_query)
#             return JsonResponse({"fulfillmentText": answer})

#     # ✅ If method not POST
#     return JsonResponse({"error": "Invalid request method"}, status=405)

import traceback   # add this at the top for detailed error logs

@csrf_exempt
def dialogflow_webhook(request):
    if request.method == "POST":
        try:
            body = json.loads(request.body.decode("utf-8"))
            print("📩 Raw Request Body:", body)  # log raw request
        except Exception as e:
            print("❌ Error decoding request body:", str(e))
            traceback.print_exc()
            return JsonResponse({"fulfillmentText": "⚠️ Invalid request body."}, status=400)

        user_query = body.get("queryResult", {}).get("queryText", "")
        intent = body.get("queryResult", {}).get("intent", {}).get("displayName", "")

        print(f"📝 Extracted User Query: {user_query}")
        print(f"🎯 Detected Intent: {intent}")

        answer = "Sorry, I couldn't generate a reply."

        try:
            if intent == "LLMQueryIntent":
                print(f"🚀 Handling LLMQueryIntent for: {user_query}")
                answer = query_with_timeout(
                    user_query,
                    website_content=get_pages_content(),
                    products=Product.objects.all()[:50],
                    brands=", ".join(get_brands()),
                    timeout=4
                )
                print("✅ Gemini Answer:", answer)

                if not answer or "⏳" in answer:
                    print("⚠️ Gemini Timeout or Empty Response")
                    return JsonResponse({
                        "fulfillmentText": "⏳ Thoda waqt lag raha hai, lekin mai aapko best shoes suggest karta hoon..."
                    })

                if not answer or len(answer.strip()) < 5:
                    print("⚠️ Gemini returned short/empty response")
                    answer = "Maaf kijiye! Aapke liye sahi jawab nahi mila, lekin mai aapko kuch best shoes suggest kar sakta hoon 👉 https://royaltrend.pk"

            else:
                print(f"🤖 Default Fallback Handling for query: {user_query}")
                answer = smart_query_handler(user_query)
                print("✅ Smart Handler Answer:", answer)

        except Exception as e:
            print("❌ Exception while processing intent:", str(e))
            traceback.print_exc()
            answer = "⚠️ Kuch problem hui, lekin aap hamari website https://royaltrend.pk par check kar sakte ho."

        # ✅ Final log before sending response
        print(f"📤 Final Reply to User: {answer}")
        return JsonResponse({"fulfillmentText": answer})

    # Invalid method log
    print("⚠️ Invalid HTTP Method:", request.method)
    return JsonResponse({"error": "Invalid request method"}, status=405)
