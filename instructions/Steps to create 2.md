**Do NOT build three separate systems.** That is the road to madness.

If you build three separate calculators, when you need to update the logic (e.g., "add a 5% discount feature"), you will have to do the work three times.

You should build **one single calculation engine** that accepts a "Price Book ID" (or Region) as an input.

Here is exactly how to handle the three books to create a system like Sensei Digital.

### 1. The "Why" (Analyze your Data)
I looked at your OCR data. You have **Regional Price Books**.
*   **Book 1 (Page 28 of first PDF):** Pricing for `ID, MT, UT, WY, OR, WA, NV, CO(NW)`.
*   **Book 3 (Page 23 of third PDF):** Pricing for `NM` (New Mexico).

The *structures* are identical (same building styles, same width/length increments), but the *dollar amounts* are different.
*   *Example:* 12x21 Regular Style is **$2,595** in Book 1, but **$2,995** (or similar) in Book 3.

### 2. The Database Strategy (The "Multi-Book" Schema)
Instead of hardcoding prices, your database needs an extra column called `region_id`.

**Bad Way (Hardcoded):**
```javascript
if (width == 12 && length == 21) {
  return 2595; 
}
```

**Good Way (Database Lookup):**
You need a table structure like this:

| region_code | style | width | length | price |
| :--- | :--- | :--- | :--- | :--- |
| **NW** | Regular | 12 | 21 | 2595 |
| **NW** | Regular | 18 | 21 | 2795 |
| **NM** | Regular | 12 | 21 | 2995 |
| **NM** | Regular | 18 | 21 | 3195 |

### 3. How to Execute This (Step-by-Step)

#### Step A: Create a "Master Template" CSV
Create one Excel sheet that represents **one** book perfectly. Define every variable.
*   Columns: `Style`, `Gauge`, `Width`, `Length`, `Height`, `Base_Price`, `Height_Adder`, `Wind_Load`.

#### Step B: Data Entry (The Boring Part)
You must convert those 3 PDFs into 3 CSV files using that template.
1.  **Book_NW.csv** (Enter data from the ID/MT/WA book)
2.  **Book_NM.csv** (Enter data from the NM book)
3.  **Book_South.csv** (Or whatever the 3rd book represents)

*Tip: Use the OCR text you have to copy-paste into Excel, but verify the numbers manually. OCR often mistakes `$5,695` for `$5,605`.*

#### Step C: The "Zip Code" Logic
In the Sensei Digital example, notice how the first thing they ask for is **Zip Code**? This is why.

1.  Create a mapping table:
    *   Zip Codes 83000-83999 (Idaho) = **Region NW**
    *   Zip Codes 87000-88499 (New Mexico) = **Region NM**
2.  When the user enters their Zip, your app sets a global variable: `currentPriceBook = 'NM'`.

#### Step D: The Code Logic
Your code simply queries the database based on that variable.

```javascript
function getBasePrice(region, style, width, length) {
   // This looks up the price in the specific "book" for that region
   let priceRow = database.find(row => 
       row.region_code === region && 
       row.style === style && 
       row.width === width && 
       row.length === length
   );
   return priceRow.price;
}

// Usage
// User is in Idaho (NW region) looking for a 12x21
let total = getBasePrice('NW', 'Regular', 12, 21); // Returns 2595
```

### 4. Handling Options across Books
Are the window prices the same in all books?
*   If **Yes**: Put windows/doors in a global `Accessories` table.
*   If **No**: Include `region_code` in your `Accessories` table too.

**Example Accessories Table:**
| item_name | region_code | price |
| :--- | :--- | :--- |
| 30x30 Window | ALL | 200 |
| Walk-in Door | NW | 350 |
| Walk-in Door | NM | 400 |

### Summary
1.  **Do not** create separate apps.
2.  Create **one** database schema.
3.  Add a `region` column to every price table.
4.  Build a **Zip Code Lookup** at the very beginning of your demo to select the correct "Book" (Region) to pull numbers from.

This makes your system scalable. When the client gives you a 4th book for Florida next year, you just import a new CSV tagged "FL" and you're done—no coding required.