long long results[8];
int main(){
    double a=2.5, b=0.5;
    results[0]=(int)(a+b);     /* 3  */
    results[1]=(int)(a*4.0);   /* 10 */
    results[2]=(int)(a/b);     /* 5  */
    results[3]=(a>b);          /* 1  */
    double d=-1.25;
    results[4]=(int)(d*2.0);   /* -2  negative double */
    int i=3;
    results[5]=(int)(a*i);     /* 7   mixed double*int (2.5*3=7.5) */
    float f=1.5f;
    results[6]=(int)(a+f);     /* 4   mixed f64+f32 */
    results[7]=(a==2.5);       /* 1 */
    return 1;
}
