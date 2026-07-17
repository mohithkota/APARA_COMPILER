int arr[5]={10,20,30,40,50}; long long results[1];
int main(){ int *p=arr; int s=0,i; for(i=0;i<5;i++) s+=*(p+i); results[0]=s; return 1; }
