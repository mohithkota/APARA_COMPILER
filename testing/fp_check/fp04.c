long long results[8];
int main(){
    float a=1.5f, b=2.5f, c=1.5f, n=-3.5f;
    results[0]=(a<b);    /* 1 */
    results[1]=(b<a);    /* 0 */
    results[2]=(a==c);   /* 1 */
    results[3]=(a!=b);   /* 1 */
    results[4]=(n<a);    /* 1  negative operand */
    results[5]=(a<=c);   /* 1 */
    results[6]=(b>=a);   /* 1 */
    float x=20.0f; int cnt=0;
    while(x>=1.0f){ x=x/2.0f; cnt=cnt+1; }
    results[7]=cnt;      /* 5: 20,10,5,2.5,1.25 then 0.625<1 */
    return 1;
}
