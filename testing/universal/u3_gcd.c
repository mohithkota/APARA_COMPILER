long long results[4];
int gcd(int a,int b){ while(b){int t=b; b=a%b; a=t;} return a; }
int main(){
    results[0]=gcd(48,36); results[1]=gcd(17,5);
    results[2]=gcd(100,80); results[3]=(long long)48*36/gcd(48,36); /* 12,1,20,144 */
    return 1;
}
