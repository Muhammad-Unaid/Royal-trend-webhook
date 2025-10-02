from django.core.management.base import BaseCommand
from bot.models import Product
import requests
from bs4 import BeautifulSoup
import csv

class Command(BaseCommand):
    help = "Scrape Shopify products + pages and save to database and CSV"

    def handle(self, *args, **kwargs):
        # 1️⃣ PRODUCTS SCRAPING
        base_url = "https://royaltrend.pk/collections/all/products.json?limit=250&page="
        page = 1
        total_products = 0

        Product.objects.all().delete()  # old data delete (optional)

        all_products = []  # store for CSV export

        while True:
            url = f"{base_url}{page}"
            response = requests.get(url)
            if response.status_code != 200:
                self.stdout.write(self.style.ERROR(f"❌ Failed to fetch page {page}"))
                break

            products = response.json().get("products", [])
            if not products:
                self.stdout.write(self.style.SUCCESS("✅ No more products found."))
                break

            for p in products:
                product_obj = Product.objects.create(
                    title=p["title"],
                    price=p["variants"][0]["price"],
                    image_url=p["images"][0]["src"] if p["images"] else "",
                    product_link=f"https://royaltrend.pk/products/{p['handle']}"
                )
                all_products.append([
                    product_obj.title,
                    product_obj.price,
                    product_obj.image_url,
                    product_obj.product_link
                ])

            total_products += len(products)
            self.stdout.write(self.style.SUCCESS(f"✅ Page {page} scraped ({len(products)} products)."))
            page += 1

        self.stdout.write(self.style.SUCCESS(f"🎯 Total {total_products} products saved."))

        # 🔄 Export to CSV
        if all_products:
            with open("products.csv", "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(["Title", "Price", "Image_URL", "Product_Link"])
                writer.writerows(all_products)
            self.stdout.write(self.style.SUCCESS("✅ Products exported to products.csv"))

        # 2️⃣ STATIC PAGES SCRAPING
        static_pages = [
            "https://royaltrend.pk/pages/about",
            "https://royaltrend.pk/pages/contact",
            "https://royaltrend.pk/collections/trending",
            "https://royaltrend.pk/collections/new-arrivals",
            "https://royaltrend.pk/collections/sale",
            "https://royaltrend.pk/collections",
            "https://royaltrend.pk/search"
        ]

        with open("pages_content.txt", "w", encoding="utf-8") as f:
            for page_url in static_pages:
                resp = requests.get(page_url)
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    page_text = soup.get_text(separator="\n", strip=True)
                    f.write(f"==== {page_url} ====\n{page_text}\n\n")
                    self.stdout.write(self.style.SUCCESS(f"📄 Saved content from: {page_url}"))
                else:
                    self.stdout.write(self.style.ERROR(f"❌ Failed to fetch: {page_url}"))
        
        self.stdout.write(self.style.SUCCESS("✅ Static pages content saved to pages_content.txt"))
