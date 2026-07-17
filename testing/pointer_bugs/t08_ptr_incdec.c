int arr[5]={10,20,30,40,50}; long long results[3];
int main(){ int *p=arr; p++; results[0]=*p; ++p; results[1]=*p; p--; results[2]=*p; return 1; }
