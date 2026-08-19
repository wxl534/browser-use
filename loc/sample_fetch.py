import requests
r = requests.get("https://www.loc.gov/search/?in=&q=china+buddhist&new=true", headers={"User-Agent":"scrapling-smoke/1.0"})
open("sample_loc_search.html","wb").write(r.content)
print(len(r.content))
