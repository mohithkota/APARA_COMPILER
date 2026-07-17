long long results[3];
int main(){ int a=5,b=3; results[0]=a>b?a:b; results[1]=(a>b?1:0)+(b>a?1:0); results[2]=a>b?(b>0?10:20):30; return 1; } /*5,1,10*/
