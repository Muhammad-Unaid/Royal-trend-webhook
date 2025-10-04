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

@csrf_exempt
def dialogflow_webhook(request):
    if request.method == "POST":
        try:
            body = json.loads(request.body.decode("utf-8"))
        except Exception:
            return JsonResponse({"fulfillmentText": "⚠️ Invalid request body."}, status=400)

        user_query = body.get("queryResult", {}).get("queryText", "")
        intent = body.get("queryResult", {}).get("intent", {}).get("displayName", "")

        answer = "Sorry, I couldn't generate a reply."

        # --- Intent Handling ---

        # ✅ LLM Query (always highest priority)
        if intent == "LLMQueryIntent":
            try:
                print(f"📝 User Query: {user_query}")
                answer = query_with_timeout(
                    user_query,
                    website_content=get_pages_content(),
                    products=Product.objects.all()[:50],
                    brands=", ".join(get_brands()),
                    timeout=4
                )
                if not answer or "⏳" in answer:
                    return JsonResponse({
                        "fulfillmentText": "⏳ Thoda waqt lag raha hai, lekin mai aapko best shoes suggest karta hoon..."
                    })
                if not answer or len(answer.strip()) < 5:
                    answer = "Maaf kijiye! Aapke liye sahi jawab nahi mila, lekin mai aapko kuch best shoes suggest kar sakta hoon 👉 https://royaltrend.pk"
            except Exception as e:
                print("❌ Error in LLMQueryIntent:", str(e))
                answer = "⚠️ Kuch problem hui, lekin aap hamari website https://royaltrend.pk par check kar sakte ho."

        elif intent == "About Website":
            return JsonResponse({
                "fulfillmentMessages": [
                    {
                        "text": {
                            "text": [
                                "👟 Royal Trend (royaltrend.pk) is a vibrant Pakistani online shoe store offering a wide variety of stylish footwear — Adidas, Nike, Skechers, Hoka, Air Jordan, Balmain aur bohot brands! 🇵🇰\n\n🔥 Flash Sale: Up to 30% Off + Free Shipping across Pakistan 🚚\n\n🥿 Sneakers, Loafers, Slides sab available hain with **Cash on Delivery (COD)**.\n\nAap hamari website visit karein 👉 https://royaltrend.pk"
                            ]
                        }
                    },
                    {
                        "payload": {
                            "richContent": [
                                [
                                    {
                                        "type": "button",
                                        "icon": {
                                            "type": "chevron_right",
                                            "color": "#4285F4"
                                        },
                                        "text": "🌐 Visit Website",
                                        "link": "https://royaltrend.pk"
                                    }
                                ]
                            ]
                        }
                    }
                ]
            })


        elif intent == "Sale":
            return JsonResponse({
                "fulfillmentMessages": [
                    {
                        "payload": {
                            "richContent": [
                                [
                                    {
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Pure_Gel_Keyano_30_Grey_right_profile.png?v=1739308410",
                                        "type": "image",
                                        "accessibilityText": "Pure Gel Keyano 30 Grey – Gel Cushioning Running Shoes"
                                    },
                                    {
                                        "actionLink": "https://royaltrend.pk/collections/sale",
                                        "type": "info",
                                        "subtitle": "Experience ultimate comfort with the Pure Gel Keyano 30 Grey. Engineered with gel cushioning, breathable mesh uppers...",
                                        "title": "Pure Gel Keyano 30 Grey – Gel Cushioning Running Shoes"
                                    }
                                ],
                                [
                                    {
                                        "accessibilityText": "NK AJ Courtside 23 Grey Fog",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Untitleddesign.png?v=1733272068",
                                        "type": "image"
                                    },
                                    {
                                        "subtitle": "Step into effortless style with the Jordan Flight Origin 4 'Light Grey/Beige', a sneaker that perfectly balances contemporary...",
                                        "actionLink": "https://royaltrend.pk/collections/sale",
                                        "type": "info",
                                        "title": "NK AJ Courtside 23 Grey Fog"
                                    }
                                ],
                                [
                                    {
                                        "type": "image",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/17_11.png?v=1732966072",
                                        "accessibilityText": "NB Fresh Foam X More Trail V3 Grey"
                                    },
                                    {
                                        "subtitle": "New Balance Running Shoes | Comfort & Style Combined. Step into premium comfort with New Balance Grey running...",
                                        "type": "info",
                                        "title": "NB Fresh Foam X More Trail V3 Grey",
                                        "actionLink": "https://royaltrend.pk/collections/sale"
                                    }
                                ],
                                [
                                    {
                                        "type": "image",
                                        "accessibilityText": "Nike React Infinity Run Flyknit 3 - Maximum Comfort & Style",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Right_side_profile_of_Nike_React_Infinity_Run_Flyknit_3_displaying.png?v=1740429742"
                                    },
                                    {
                                        "subtitle": "Experience ultimate comfort and style with the Nike React Infinity Run Flyknit 3. Designed for everyday runs...",
                                        "actionLink": "https://royaltrend.pk/collections/sale",
                                        "type": "info",
                                        "title": "Nike React Infinity Run Flyknit 3 - Maximum Comfort & Style"
                                    }
                                ],
                                [
                                    {
                                        "accessibilityText": "Balmain Slide Box Beige Black – Royal Trend Pakistan",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Right_Side_View_Balmain.jpg?v=1741889991",
                                        "type": "image"
                                    },
                                    {
                                        "title": "Balmain Slide Box Beige Black – Royal Trend Pakistan",
                                        "type": "info",
                                        "actionLink": "https://royaltrend.pk/collections/sale",
                                        "subtitle": "Shop Balmain Slide Box Beige Black at Royal Trend Pakistan. Luxurious, comfy, and stylish. Free shipping. Order..."
                                    }
                                ]
                            ]
                        }
                    }
                ]
            })

        elif intent == "Trending":
            return JsonResponse({
                "fulfillmentMessages": [
                    {
                        "payload": {
                            "richContent": [
                                [
                                    {
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Untitleddesign_36.png?v=1733154502",
                                        "type": "image",
                                        "accessibilityText": "Aiir Maxx 90 Black Red – Premium Comfort & Bold Style"
                                    },
                                    {
                                        "type": "info",
                                        "title": "Aiir Maxx 90 Black Red – Premium Comfort & Bold Style",
                                        "actionLink": "https://royaltrend.pk/collections/trending",
                                        "subtitle": "Elevate your sneaker game with the Aiir Maxx 90 Black Red – the perfect combination of performance, comfort,..."
                                    }
                                ],
                                [
                                    {
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Untitleddesign_52.png?v=1733155771",
                                        "accessibilityText": "Aiirmaxx 90 White Black",
                                        "type": "image"
                                    },
                                    {
                                        "actionLink": "https://royaltrend.pk/collections/trending",
                                        "title": "Aiirmaxx 90 White Black",
                                        "subtitle": "Aiir Maxx White Edition Sneakers - Sleek, Minimalist, and IconicStep into elegance and unmatched comfort with the Aiir...",
                                        "type": "info"
                                    }
                                ],
                                [
                                    {
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Right_profile_of_NAF_1_Low_white_shoes.png?v=1739306944",
                                        "accessibilityText": "NAF 1 Low – Iconic All-White Classic Sneakers",
                                        "type": "image"
                                    },
                                    {
                                        "type": "info",
                                        "title": "NAF 1 Low – Iconic All-White Classic Sneakers",
                                        "actionLink": "https://royaltrend.pk/collections/trending",
                                        "subtitle": "Step into timeless style with Royal Trend’s NAF 1 Low All-White Classic Sneakers. Crafted for unmatched comfort..."
                                    }
                                ],
                                [
                                    {
                                        "accessibilityText": "Adii Retropy E5 Camel - Premium Quality Sneakers for Ultimate Comfort",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/17_27.png?v=1732967385",
                                        "type": "image"
                                    },
                                    {
                                        "subtitle": "Elevate your style with the Adii Retropy E5 Camel, a pair of sneakers that effortlessly combine premium...",
                                        "actionLink": "https://royaltrend.pk/collections/trending",
                                        "type": "info",
                                        "title": "Adii Retropy E5 Camel - Premium Quality Sneakers for Ultimate Comfort"
                                    }
                                ]
                            ]
                        }
                    }
                ]
            })

        elif intent == "New Arrivals":
            return JsonResponse({
                "fulfillmentMessages": [
                    {
                        "payload": {
                            "richContent": [
                                [
                                    {
                                        "type": "image",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Adii_Yeeezzzy_350_Earth_sneakers_right_side_profile.png?v=1739222982",
                                        "accessibilityText": "Men’s & Women’s Gym, Running & Casual Shoes | Royal Trend Pakistan"
                                    },
                                    {
                                        "type": "info",
                                        "title": "Men’s & Women’s Gym, Running & Casual Shoes | Royal Trend Pakistan",
                                        "subtitle": "Upgrade your footwear with the Adii Yeeezzzy 350 Earth sneakers. Featuring breathable knit uppers, cloud foam cushioning...",
                                        "actionLink": "https://royaltrend.pk/collections/new-arrivals"
                                    }
                                ],
                                [
                                    {
                                        "type": "image",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/NB_v5_Fresh_Foam_X_More_Blue_right_profile.png?v=1739225047",
                                        "accessibilityText": "NB v5 Fresh Foam X More Blue – Premium Cushioned Running Shoes w/ Breathable Mesh"
                                    },
                                    {
                                        "type": "info",
                                        "title": "NB v5 Fresh Foam X More Blue – Premium Cushioned Running Shoes w/ Breathable Mesh",
                                        "subtitle": "Experience unmatched comfort with the NB v5 Fresh Foam X More Blue running shoes. Featuring breathable mesh...",
                                        "actionLink": "https://royaltrend.pk/collections/new-arrivals"
                                    }
                                ],
                                [
                                    {
                                        "type": "image",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Untitleddesign-2025-01-24T215829.715.png?v=1738061787",
                                        "accessibilityText": "ADii Avrynn Boost (Olive)"
                                    },
                                    {
                                        "type": "info",
                                        "title": "ADii Avrynn Boost (Olive)",
                                        "subtitle": "Make a bold statement with the Cordura Bounce Sneakers in an earthy olive green color. Designed for...",
                                        "actionLink": "https://royaltrend.pk/collections/new-arrivals"
                                    }
                                ],
                                [
                                    {
                                        "type": "image",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/SK_Max_Protect_Waterproof_Grey_right_profile.png?v=1739385943",
                                        "accessibilityText": "Men’s & Women’s All-Weather Footwear for Hiking, Rain & Pakistani Terrain | Royal Trend Pakistan"
                                    },
                                    {
                                        "type": "info",
                                        "title": "Men’s & Women’s All-Weather Footwear for Hiking, Rain & Pakistani Terrain | Royal Trend Pakistan",
                                        "subtitle": "Brave Pakistan’s monsoon with SK Max Protect Waterproof Grey. Built with a breathable waterproof membrane, anti-slip rubber...",
                                        "actionLink": "https://royaltrend.pk/collections/new-arrivals"
                                    }
                                ],
                                [
                                    {
                                        "type": "image",
                                        "rawUrl": "https://royaltrend.pk/cdn/shop/files/Sketch_Max_Cushion_Slide_Black_Side_profile.jpg?v=1739220435",
                                        "accessibilityText": "Unisex Comfort for Home, Gym & Summer | Royal Trend"
                                    },
                                    {
                                        "type": "info",
                                        "title": "Unisex Comfort for Home, Gym & Summer | Royal Trend",
                                        "subtitle": "Experience all-day comfort with Sketch Max Cushion Slide Black. Designed with orthopedic cloud foam and ergonomic arch...",
                                        "actionLink": "https://royaltrend.pk/collections/new-arrivals"
                                    }
                                ]
                            ]
                        }
                    }
                ]
            })

        elif intent == "helpline":
            return JsonResponse({
                "fulfillmentMessages": [
                    {
                        "text": {
                            "text": [
                                "📞 Our helpline number is: 02138899998\nFeel free to call us anytime during business hours. We're here to help! 😊"
                            ]
                        }
                    },
                    {
                        "payload": {
                            "richContent": [
                                [
                                    {
                                        "icon": {
                                            "type": "chevron_right",
                                            "color": "#25D366"
                                        },
                                        "text": "📱 WhatsApp",
                                        "type": "button",
                                        "link": "https://wa.me/923151179953"   # ✅ Replace with your WhatsApp link
                                    }
                                ]
                            ]
                        }
                    }
                ]
            })

        # ✅ Default Fallback Intent (runs when none match)
        else:
            answer = smart_query_handler(user_query)
            return JsonResponse({"fulfillmentText": answer})

    # ✅ If method not POST
    return JsonResponse({"error": "Invalid request method"}, status=405)
