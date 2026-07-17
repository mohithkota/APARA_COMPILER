int arr[4]={10,20,30,40}; long long results[2];
int *pick(int *a,int i){ return a+i; }
int main(){ int *p=pick(arr,2); results[0]=*p; results[1]=p[1]; return 1; }
