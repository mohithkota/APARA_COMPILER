long long results[4];
int main(){
    float a=3.5f, b=1.5f;
    results[0]=(int)(a+b);      /* 5  */
    results[1]=(int)(a-b);      /* 2  */
    results[2]=(int)(a*b);      /* 5  (3.5*1.5=5.25 -> 5) */
    float c=10.0f, d=4.0f;
    results[3]=(int)(c/d);      /* 2  (2.5 -> 2) */
    return 1;
}
