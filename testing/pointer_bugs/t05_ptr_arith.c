int arr[5]={10,20,30,40,50}; long long results[4];
int main(){ int *p=arr; results[0]=*(p+2); results[1]=*(arr+3); p=p+1; results[2]=*p; results[3]=p[2]; return 1; }
