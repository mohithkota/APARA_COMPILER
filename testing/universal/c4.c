long long results[4];
int main(){ int a=5,b=5,i=0; while(a>0 && b>0){ results[i]=a*10+b; a--; b--; i++; if(i>=4) break; } return 1; }
