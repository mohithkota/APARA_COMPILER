long long results[5];
int isprime[30];
int main(){
    int i,j,n=0; for(i=0;i<30;i++) isprime[i]=1; isprime[0]=isprime[1]=0;
    for(i=2;i<30;i++) if(isprime[i]) for(j=i*i;j<30;j+=i) isprime[j]=0;
    for(i=2;i<30 && n<5;i++) if(isprime[i]) results[n++]=i;  /* 2,3,5,7,11 */
    return 1;
}
