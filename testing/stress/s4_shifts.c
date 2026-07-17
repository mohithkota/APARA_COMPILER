long long results[12];
int main(){
    long long x=1; int n=5;
    unsigned long long u=0xF000000000000000ull;
    results[0]=x<<0; results[1]=x<<1; results[2]=x<<31; results[3]=x<<62;
    results[4]=x<<n; results[5]=(1<<n);
    results[6]=(long long)u>>4;         /* arithmetic (sign) */
    results[7]=(long long)(u>>4);       /* logical */
    results[8]=255>>3; results[9]=(-256)>>4;
    results[10]=x<<(n+2); results[11]=1024>>n;
    return 1;
}
