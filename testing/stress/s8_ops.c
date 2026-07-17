long long results[16];
long long __nand(long long,long long);
long long __nor(long long,long long);
long long __xnor(long long,long long);
int main(){
    int a=0xF0, b=0x3C;
    results[0]=a&b; results[1]=a|b; results[2]=a^b;
    results[3]=__nand(a,b); results[4]=__nor(a,b); results[5]=__xnor(a,b);
    results[6]=a==b; results[7]=a!=b; results[8]=a<b; results[9]=a>=b;
    results[10]=(a&&b); results[11]=(a||0); results[12]=!0; results[13]=!a;
    results[14]=(a>b)?100:200; results[15]=a+b*2-a/2;
    return 1;
}
