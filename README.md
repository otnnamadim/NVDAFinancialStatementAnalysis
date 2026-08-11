# NVIDIA ($NVDA) Financial Statement & Segment Analysis

## Overview & Accounting Relevance

NVIDIA is one of the most popular companies in the world currently, and it sits at the center of the semiconductor supply chain and artificial intelligence infrastructure. My goal in building this dashboard was to illustrate the financials in a simplified manner to  better understand how its business model functions and to summarize the key financial results over time. This project constructs an automated data extraction and transformation pipeline to disaggregate NVIDIA's revenue models across geographic markets and operating segments (e.g., Data Center, Gaming, Professional Visualization).

By analyzing the company’s footnote 13 which details its reportable segments, this repository demonstrates how public companies disaggregate accounting data. As a result, it’s easier to visually identify their level of concentration risk, the impact of trade policy on performance, and general trends in revenue.

## Data Pipeline & Methodological Architecture
* **Data Retrieval:** Programmatically ingests SEC EDGAR filing data utilizing the SEC’s Company Facts API and XBRL taxonomy parsing.
* **Segment Breakdown:** Processes embedded XBRL segment tags to isolate disaggregated revenue streams by reporting unit and geographic origin.
* **Transformation & Forecasting:** Cleans and normalizes unstructured financial concepts into structured Pandas DataFrames for trend analysis, margin evaluation, and baseline forward-looking forecasts.

## Academic & Research Connection
This project feeds into broader research interests in finance, supply chain, and capital allocation. From the research I’ve read to date, I’ve noted that analyzing geographic disaggregation is central to evaluating transfer pricing, international tax policy adjustments, and firm-level economic exposure to trade regulation.

---

## Getting Started

### Prerequisites
* Python 3.9+
* Required Libraries: `pandas`, `requests`, `matplotlib`, `seaborn`

### Installation & Execution
1. Clone the repository:
   ```bash
   git clone [https://github.com/otnnamadim/NVDAFinancialStatementAnalysis.git](https://github.com/otnnamadim/NVDAFinancialStatementAnalysis.git)
   cd NVDAFinancialStatementAnalysis

2. Set up your SEC User-Agent header (required by SEC EDGAR API):
   ```bash
   # In your script/config file:
   headers = {'User-Agent': 'YourName contact@domain.com'}

3. Run the analysis script:
   ```bash
   python main.py
