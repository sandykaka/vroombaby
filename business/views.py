from django.http import HttpResponse
from django.shortcuts import render

# Create your views here.
def index(request):
    return render(request, 'business/index.html')

def products(request):
    return render(request, 'business/products.html')

def googleeb914ff572b518f7(request):
    return HttpResponse('business/googleeb914ff572b518f7.html')

def support_view(request):
    return render(request, 'business/support.html')