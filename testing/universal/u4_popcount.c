long long results[4];
int popcount(unsigned int x){ int c=0; while(x){c+=x&1; x=x>>1;} return c; }
int main(){
    results[0]=popcount(255); results[1]=popcount(1024);
    results[2]=popcount(0); results[3]=popcount(0xF0F0);  /* 8,1,0,8 */
    return 1;
}
