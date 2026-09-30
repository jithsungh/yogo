import requests
from bs4 import BeautifulSoup

def decode_secret_message(url):
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    grid = {}
    
    for row in soup.find_all('tr')[1:]:
        cols = row.find_all('td')
        if len(cols) >= 3 and cols[0].text.strip().isdigit():
            grid[(int(cols[0].text), int(cols[2].text))] = cols[1].text.strip() or ' '
            
    if not grid: return
    
    max_x, max_y = max(k[0] for k in grid), max(k[1] for k in grid)
    
    for y in range(max_y, -1, -1):
        print("".join(grid.get((x, y), ' ') for x in range(max_x + 1)))

decode_secret_message("https://docs.google.com/document/d/e/2PACX-1vSvM5gDlNvt7npYHhp_XfsJvuntUhq184By5xO_pA4b_gCWeXb6dM6ZxwN8rE6S4ghUsCj2VKR21oEP/pub")
